# -*- coding: utf-8 -*-
"""
painel_agentes/wms.py

Endereçamento do galpão (primeira peça do WMS próprio, Hugo 04/09/2026).
Módulo de dados puro (sem Flask): tabelas wms_*, posições, operadores,
produtos, movimentos, saldos e etiquetas em PDF.

Regras de negócio que este módulo garante:
- Código de posição canônico: ÁREA-E<n>-N<n> (estante/nível) ou ÁREA-P<n>
  (pallet). Ex.: C5-E3-N2, C5-P3, CG1-E1-N4. Sempre maiúsculo, sem zero à
  esquerda. O QR da etiqueta carrega "POS:<código>".
- O movimento é a verdade: wms_movimentos só recebe INSERT (nunca UPDATE/
  DELETE). wms_saldos é derivada e pode ser reconstruída do zero por
  recalcular_saldos(). Corrigir = registrar um AJUSTE.
- Cada movimento nasce com um uuid gerado no aparelho; reenviar o mesmo
  uuid (fila offline) devolve o movimento já gravado, sem duplicar.
- Validade é obrigatória na ENTRADA quando produto.controla_validade=1
  (padrão pra todos; exceção por produto). Lote é sempre opcional.
- Quantidade é sempre gravada na unidade base do produto (UN). A UI pode
  receber caixas e multiplicar por qtd_por_caixa antes de mandar.
"""
import hashlib
import hmac
import io
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"

PREFIXO_QR = "POS:"
TEMPERATURAS = ("AMBIENTE", "REFRIGERADO", "CONGELADO")
TIPOS_AREA = {
    "CONTAINER": ("Container", "AMBIENTE"),
    "CAM_CONGELADA": ("Câmara congelada", "CONGELADO"),
    "CAM_REFRIGERADA": ("Câmara refrigerada", "REFRIGERADO"),
}
TIPOS_MOVIMENTO = ("ENTRADA", "SAIDA", "TRANSFERENCIA", "AJUSTE", "INVENTARIO")

RE_AREA = re.compile(r"^[A-Z]{1,3}[1-9]\d?$")
RE_POSICAO = re.compile(r"^([A-Z]{1,3}[1-9]\d?)-(?:E([1-9]\d?)-N([1-9]\d?)|P([1-9]\d{0,2}))$")
RE_EAN = re.compile(r"^\d{8}$|^\d{12,14}$")

PBKDF2_ITERACOES = 200_000
MAX_TENTATIVAS_PIN = 5
BLOQUEIO_MINUTOS = 15
SESSAO_OPERADOR_HORAS = 2

# Estrutura inicial informada pelo Hugo (04/09): 6 containers com 5
# estantes de 4 níveis, exceto o C1 que tem estantes dos dois lados (10).
# Pallets por container ainda não informados -- criam-se pela tela.
# Câmaras nascem sem posições (modelo ainda não definido no galpão).
ESTRUTURA_INICIAL = {
    "areas": [
        ("C1", "CONTAINER", 10, 4), ("C2", "CONTAINER", 5, 4), ("C3", "CONTAINER", 5, 4),
        ("C4", "CONTAINER", 5, 4), ("C5", "CONTAINER", 5, 4), ("C6", "CONTAINER", 5, 4),
        ("CG1", "CAM_CONGELADA", 0, 0), ("CG2", "CAM_CONGELADA", 0, 0),
        ("CR1", "CAM_REFRIGERADA", 0, 0), ("CR2", "CAM_REFRIGERADA", 0, 0),
    ],
}


class ErroWMS(ValueError):
    """Erro de regra de negócio, com mensagem pronta pra mostrar na tela."""


_DDL = """
CREATE TABLE IF NOT EXISTS wms_areas (
    codigo       TEXT PRIMARY KEY,
    tipo         TEXT NOT NULL,
    temperatura  TEXT NOT NULL,
    nome         TEXT NOT NULL,
    ativo        INTEGER NOT NULL DEFAULT 1,
    criado_em    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS wms_posicoes (
    codigo       TEXT PRIMARY KEY,
    area_codigo  TEXT NOT NULL REFERENCES wms_areas(codigo),
    tipo         TEXT NOT NULL,
    estante      INTEGER,
    nivel        INTEGER,
    pallet       INTEGER,
    ativo        INTEGER NOT NULL DEFAULT 1,
    criado_em    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    criado_por   TEXT
);
CREATE INDEX IF NOT EXISTS idx_wms_posicoes_area ON wms_posicoes(area_codigo);
CREATE TABLE IF NOT EXISTS wms_produtos (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    stokki_id             INTEGER UNIQUE,
    sku                   TEXT,
    descricao             TEXT NOT NULL,
    embarcador            TEXT,
    embarcador_id         INTEGER,
    ean                   TEXT,
    dun                   TEXT,
    categoria             TEXT,
    unidade               TEXT NOT NULL DEFAULT 'UN',
    qtd_por_caixa         REAL,
    peso_liquido_kg       REAL,
    controla_lote         INTEGER NOT NULL DEFAULT 1,
    controla_validade     INTEGER NOT NULL DEFAULT 1,
    ativo                 INTEGER NOT NULL DEFAULT 1,
    origem                TEXT NOT NULL DEFAULT 'STOKKI',
    stokki_atualizado_em  TEXT,
    perfil_lido_em        TEXT,
    atualizado_em         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_wms_produtos_ean ON wms_produtos(ean);
CREATE INDEX IF NOT EXISTS idx_wms_produtos_dun ON wms_produtos(dun);
CREATE INDEX IF NOT EXISTS idx_wms_produtos_sku ON wms_produtos(sku);
CREATE TABLE IF NOT EXISTS wms_operadores (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nome           TEXT NOT NULL UNIQUE,
    pin_hash       TEXT NOT NULL,
    pin_salt       TEXT NOT NULL,
    tentativas_pin INTEGER NOT NULL DEFAULT 0,
    bloqueado_ate  TEXT,
    ativo          INTEGER NOT NULL DEFAULT 1,
    criado_em      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS wms_movimentos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid            TEXT NOT NULL UNIQUE,
    tipo            TEXT NOT NULL,
    produto_id      INTEGER NOT NULL REFERENCES wms_produtos(id),
    lote            TEXT NOT NULL DEFAULT '',
    validade        TEXT,
    quantidade      REAL NOT NULL,
    posicao_origem  TEXT,
    posicao_destino TEXT,
    operador_id     INTEGER,
    operador_nome   TEXT,
    dispositivo     TEXT,
    observacao      TEXT,
    criado_em       TEXT NOT NULL,
    recebido_em     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_wms_mov_produto ON wms_movimentos(produto_id, id);
CREATE INDEX IF NOT EXISTS idx_wms_mov_origem ON wms_movimentos(posicao_origem);
CREATE INDEX IF NOT EXISTS idx_wms_mov_destino ON wms_movimentos(posicao_destino);
CREATE TABLE IF NOT EXISTS wms_saldos (
    posicao        TEXT NOT NULL,
    produto_id     INTEGER NOT NULL,
    lote           TEXT NOT NULL DEFAULT '',
    validade       TEXT NOT NULL DEFAULT '',
    quantidade     REAL NOT NULL DEFAULT 0,
    atualizado_em  TEXT NOT NULL,
    PRIMARY KEY (posicao, produto_id, lote, validade)
);
CREATE INDEX IF NOT EXISTS idx_wms_saldos_produto ON wms_saldos(produto_id);
"""


def agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    caminho = Path(caminho) if caminho else DB_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_DDL)
    return conn


# ── Posições ─────────────────────────────────────────────────────────────────

def normalizar_codigo(codigo: str) -> str:
    codigo = str(codigo or "").strip().upper()
    if codigo.startswith(PREFIXO_QR):
        codigo = codigo[len(PREFIXO_QR):]
    return codigo.replace(" ", "").replace("_", "-")


def parse_posicao(codigo: str) -> dict:
    """Valida e decompõe um código de posição. Levanta ErroWMS se inválido."""
    codigo = normalizar_codigo(codigo)
    m = RE_POSICAO.match(codigo)
    if not m:
        raise ErroWMS(
            f"Código de posição inválido: {codigo!r}. Use ÁREA-E<n>-N<n> (ex.: C5-E3-N2) ou ÁREA-P<n> (ex.: C5-P3).")
    area, estante, nivel, pallet = m.groups()
    if pallet is not None:
        return {"codigo": codigo, "area": area, "tipo": "PALLET", "estante": None, "nivel": None, "pallet": int(pallet)}
    return {"codigo": codigo, "area": area, "tipo": "ESTANTE_NIVEL", "estante": int(estante), "nivel": int(nivel), "pallet": None}


def parece_posicao(codigo: str) -> bool:
    return bool(RE_POSICAO.match(normalizar_codigo(codigo)))


def descricao_posicao(p: dict) -> str:
    if p["tipo"] == "PALLET":
        return f"Pallet {p['pallet']}"
    return f"Estante {p['estante']} · Nível {p['nivel']}"


def criar_area(conn, codigo: str, tipo: str, nome: str | None = None, temperatura: str | None = None) -> dict:
    codigo = normalizar_codigo(codigo)
    if not RE_AREA.match(codigo):
        raise ErroWMS(f"Código de área inválido: {codigo!r}. Use letras + número, ex.: C7, CG1, CR2.")
    if tipo not in TIPOS_AREA:
        raise ErroWMS(f"Tipo de área inválido: {tipo!r}.")
    rotulo, temp_padrao = TIPOS_AREA[tipo]
    temperatura = (temperatura or temp_padrao).upper()
    if temperatura not in TEMPERATURAS:
        raise ErroWMS(f"Temperatura inválida: {temperatura!r}.")
    numero = re.sub(r"\D", "", codigo)
    nome = (nome or f"{rotulo} {numero}").strip()
    existente = conn.execute("SELECT * FROM wms_areas WHERE codigo = ?", (codigo,)).fetchone()
    if existente:
        conn.execute("UPDATE wms_areas SET tipo=?, temperatura=?, nome=?, ativo=1 WHERE codigo=?",
                     (tipo, temperatura, nome, codigo))
    else:
        conn.execute("INSERT INTO wms_areas (codigo, tipo, temperatura, nome) VALUES (?,?,?,?)",
                     (codigo, tipo, temperatura, nome))
    conn.commit()
    return obter_area(conn, codigo)


def obter_area(conn, codigo: str) -> dict | None:
    row = conn.execute("SELECT * FROM wms_areas WHERE codigo = ?", (normalizar_codigo(codigo),)).fetchone()
    return dict(row) if row else None


def listar_areas(conn, apenas_ativas: bool = True) -> list[dict]:
    where = "WHERE a.ativo = 1" if apenas_ativas else ""
    rows = conn.execute(f"""
        SELECT a.*,
               (SELECT COUNT(*) FROM wms_posicoes p WHERE p.area_codigo = a.codigo AND p.ativo = 1) AS n_posicoes,
               (SELECT COUNT(DISTINCT s.posicao) FROM wms_saldos s
                  JOIN wms_posicoes p2 ON p2.codigo = s.posicao
                 WHERE p2.area_codigo = a.codigo AND s.quantidade > 0) AS n_ocupadas
          FROM wms_areas a {where}
         ORDER BY a.tipo = 'CONTAINER' DESC, a.codigo
    """).fetchall()
    return [dict(r) for r in rows]


def criar_posicao(conn, codigo: str, criado_por: str | None = None) -> dict:
    p = parse_posicao(codigo)
    if not obter_area(conn, p["area"]):
        raise ErroWMS(f"Área {p['area']} não existe. Crie a área antes da posição.")
    existente = conn.execute("SELECT ativo FROM wms_posicoes WHERE codigo = ?", (p["codigo"],)).fetchone()
    if existente:
        if not existente["ativo"]:
            conn.execute("UPDATE wms_posicoes SET ativo = 1 WHERE codigo = ?", (p["codigo"],))
            conn.commit()
        return obter_posicao(conn, p["codigo"])
    conn.execute(
        "INSERT INTO wms_posicoes (codigo, area_codigo, tipo, estante, nivel, pallet, criado_por) VALUES (?,?,?,?,?,?,?)",
        (p["codigo"], p["area"], p["tipo"], p["estante"], p["nivel"], p["pallet"], criado_por))
    conn.commit()
    return obter_posicao(conn, p["codigo"])


def gerar_posicoes(conn, area: str, estantes: int = 0, niveis: int = 0, pallets: int = 0,
                   criado_por: str | None = None) -> dict:
    """Gera em lote E1..E<estantes> × N1..N<niveis> e P1..P<pallets>. Idempotente:
    posições já existentes são contadas, não duplicadas."""
    area = normalizar_codigo(area)
    if not obter_area(conn, area):
        raise ErroWMS(f"Área {area} não existe.")
    estantes, niveis, pallets = int(estantes or 0), int(niveis or 0), int(pallets or 0)
    if estantes < 0 or niveis < 0 or pallets < 0 or estantes > 99 or niveis > 20 or pallets > 200:
        raise ErroWMS("Quantidades fora do razoável (estantes ≤ 99, níveis ≤ 20, pallets ≤ 200).")
    codigos = []
    if estantes and niveis:
        for e in range(1, estantes + 1):
            for n in range(1, niveis + 1):
                codigos.append(f"{area}-E{e}-N{n}")
    for pnum in range(1, pallets + 1):
        codigos.append(f"{area}-P{pnum}")
    criadas, existentes = [], 0
    for codigo in codigos:
        if conn.execute("SELECT 1 FROM wms_posicoes WHERE codigo = ?", (codigo,)).fetchone():
            existentes += 1
            continue
        p = parse_posicao(codigo)
        conn.execute(
            "INSERT INTO wms_posicoes (codigo, area_codigo, tipo, estante, nivel, pallet, criado_por) VALUES (?,?,?,?,?,?,?)",
            (p["codigo"], p["area"], p["tipo"], p["estante"], p["nivel"], p["pallet"], criado_por))
        criadas.append(codigo)
    conn.commit()
    return {"area": area, "criadas": criadas, "existentes": existentes}


def desativar_posicao(conn, codigo: str, ativo: bool = False) -> dict:
    codigo = normalizar_codigo(codigo)
    if not ativo:
        ocupada = conn.execute("SELECT COALESCE(SUM(quantidade),0) AS q FROM wms_saldos WHERE posicao = ?", (codigo,)).fetchone()["q"]
        if ocupada and ocupada > 0:
            raise ErroWMS(f"{codigo} ainda tem saldo ({ocupada:g}). Mova ou dê saída antes de desativar.")
    cur = conn.execute("UPDATE wms_posicoes SET ativo = ? WHERE codigo = ?", (1 if ativo else 0, codigo))
    if cur.rowcount == 0:
        raise ErroWMS(f"Posição {codigo} não existe.")
    conn.commit()
    return obter_posicao(conn, codigo)


def obter_posicao(conn, codigo: str) -> dict | None:
    row = conn.execute("""
        SELECT p.*, a.nome AS area_nome, a.temperatura, a.tipo AS area_tipo
          FROM wms_posicoes p JOIN wms_areas a ON a.codigo = p.area_codigo
         WHERE p.codigo = ?""", (normalizar_codigo(codigo),)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["descricao"] = descricao_posicao(d)
    return d


def listar_posicoes(conn, area: str | None = None, apenas_ativas: bool = True) -> list[dict]:
    cond, params = [], []
    if area:
        cond.append("p.area_codigo = ?"); params.append(normalizar_codigo(area))
    if apenas_ativas:
        cond.append("p.ativo = 1")
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    rows = conn.execute(f"""
        SELECT p.*, a.nome AS area_nome, a.temperatura,
               (SELECT COUNT(*) FROM wms_saldos s WHERE s.posicao = p.codigo AND s.quantidade > 0) AS n_itens,
               (SELECT COALESCE(SUM(s.quantidade),0) FROM wms_saldos s WHERE s.posicao = p.codigo AND s.quantidade > 0) AS quantidade
          FROM wms_posicoes p JOIN wms_areas a ON a.codigo = p.area_codigo
          {where}
         ORDER BY p.area_codigo, p.tipo = 'PALLET', p.estante, p.nivel, p.pallet
    """, params).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["descricao"] = descricao_posicao(d); out.append(d)
    return out


def semear_estrutura_inicial(conn, criado_por: str = "sistema") -> dict:
    """Cria as 10 áreas e as posições dos containers informados pelo Hugo.
    Idempotente: pode rodar a cada subida do painel."""
    resumo = {"areas": 0, "posicoes": 0}
    for codigo, tipo, estantes, niveis in ESTRUTURA_INICIAL["areas"]:
        if not obter_area(conn, codigo):
            criar_area(conn, codigo, tipo)
            resumo["areas"] += 1
        if estantes and niveis:
            r = gerar_posicoes(conn, codigo, estantes, niveis, 0, criado_por)
            resumo["posicoes"] += len(r["criadas"])
    return resumo


# ── Operadores ───────────────────────────────────────────────────────────────

def _hash_pin(pin: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERACOES).hex()


def validar_pin(pin: str) -> str:
    pin = str(pin or "").strip()
    if not re.fullmatch(r"\d{4}", pin):
        raise ErroWMS("PIN precisa ter exatamente 4 dígitos.")
    return pin


def criar_operador(conn, nome: str, pin: str) -> dict:
    nome = " ".join(str(nome or "").split())
    if not nome:
        raise ErroWMS("Nome do operador é obrigatório.")
    pin = validar_pin(pin)
    salt = secrets.token_hex(16)
    existente = conn.execute("SELECT id FROM wms_operadores WHERE nome = ? COLLATE NOCASE", (nome,)).fetchone()
    if existente:
        conn.execute("UPDATE wms_operadores SET pin_hash=?, pin_salt=?, tentativas_pin=0, bloqueado_ate=NULL, ativo=1 WHERE id=?",
                     (_hash_pin(pin, salt), salt, existente["id"]))
        oid = existente["id"]
    else:
        cur = conn.execute("INSERT INTO wms_operadores (nome, pin_hash, pin_salt) VALUES (?,?,?)",
                           (nome, _hash_pin(pin, salt), salt))
        oid = cur.lastrowid
    conn.commit()
    return obter_operador(conn, oid)


def obter_operador(conn, operador_id: int) -> dict | None:
    row = conn.execute("SELECT id, nome, ativo, criado_em FROM wms_operadores WHERE id = ?", (operador_id,)).fetchone()
    return dict(row) if row else None


def listar_operadores(conn, apenas_ativos: bool = True) -> list[dict]:
    where = "WHERE ativo = 1" if apenas_ativos else ""
    return [dict(r) for r in conn.execute(f"SELECT id, nome, ativo FROM wms_operadores {where} ORDER BY nome").fetchall()]


def desativar_operador(conn, operador_id: int, ativo: bool = False):
    conn.execute("UPDATE wms_operadores SET ativo = ? WHERE id = ?", (1 if ativo else 0, operador_id))
    conn.commit()


def autenticar_operador(conn, operador_id: int, pin: str) -> dict:
    row = conn.execute("SELECT * FROM wms_operadores WHERE id = ? AND ativo = 1", (operador_id,)).fetchone()
    if not row:
        raise ErroWMS("Operador não encontrado.")
    if row["bloqueado_ate"] and row["bloqueado_ate"] > agora():
        raise ErroWMS(f"PIN bloqueado por muitas tentativas. Tente de novo às {row['bloqueado_ate'][11:16]}.")
    pin = str(pin or "").strip()
    if not hmac.compare_digest(_hash_pin(pin, row["pin_salt"]), row["pin_hash"]):
        tentativas = (row["tentativas_pin"] or 0) + 1
        bloqueio = None
        if tentativas >= MAX_TENTATIVAS_PIN:
            bloqueio = (datetime.now() + timedelta(minutes=BLOQUEIO_MINUTOS)).strftime("%Y-%m-%d %H:%M:%S")
            tentativas = 0
        conn.execute("UPDATE wms_operadores SET tentativas_pin=?, bloqueado_ate=? WHERE id=?", (tentativas, bloqueio, row["id"]))
        conn.commit()
        if bloqueio:
            raise ErroWMS(f"PIN errado 5 vezes. Bloqueado por {BLOQUEIO_MINUTOS} minutos.")
        raise ErroWMS("PIN incorreto.")
    conn.execute("UPDATE wms_operadores SET tentativas_pin=0, bloqueado_ate=NULL WHERE id=?", (row["id"],))
    conn.commit()
    return {"id": row["id"], "nome": row["nome"]}


# ── Produtos ─────────────────────────────────────────────────────────────────

_CAMPOS_PRODUTO = ("sku", "descricao", "embarcador", "embarcador_id", "ean", "dun", "categoria", "unidade",
                   "qtd_por_caixa", "peso_liquido_kg", "controla_lote", "ativo", "stokki_atualizado_em")


def _limpar_ean(valor) -> str | None:
    digitos = re.sub(r"\D", "", str(valor or ""))
    return digitos or None


def upsert_produto_stokki(conn, dados: dict) -> int:
    """Grava/atualiza um produto vindo da tabela do Stokki (chave stokki_id).
    Só toca os campos presentes em `dados`; campos de perfil (unidade, dun,
    qtd_por_caixa...) são preservados quando não vierem."""
    stokki_id = int(dados["stokki_id"])
    campos = {k: dados[k] for k in _CAMPOS_PRODUTO if k in dados}
    if "ean" in campos:
        campos["ean"] = _limpar_ean(campos["ean"])
    if "dun" in campos:
        campos["dun"] = _limpar_ean(campos["dun"])
    if "descricao" in campos and not campos["descricao"]:
        campos["descricao"] = campos.get("sku") or f"Produto #{stokki_id}"
    campos["atualizado_em"] = agora()
    row = conn.execute("SELECT id FROM wms_produtos WHERE stokki_id = ?", (stokki_id,)).fetchone()
    if row:
        sets = ", ".join(f"{k} = ?" for k in campos)
        conn.execute(f"UPDATE wms_produtos SET {sets} WHERE id = ?", (*campos.values(), row["id"]))
        return row["id"]
    campos.setdefault("descricao", f"Produto #{stokki_id}")
    campos["stokki_id"] = stokki_id
    campos["origem"] = "STOKKI"
    cols = ", ".join(campos)
    marks = ", ".join("?" for _ in campos)
    cur = conn.execute(f"INSERT INTO wms_produtos ({cols}) VALUES ({marks})", tuple(campos.values()))
    return cur.lastrowid


def atualizar_perfil_produto(conn, stokki_id: int, perfil: dict) -> None:
    campos = {k: perfil[k] for k in ("unidade", "qtd_por_caixa", "dun", "ean", "peso_liquido_kg", "controla_lote", "embarcador_id") if k in perfil and perfil[k] is not None}
    if "ean" in campos:
        campos["ean"] = _limpar_ean(campos["ean"])
    if "dun" in campos:
        campos["dun"] = _limpar_ean(campos["dun"])
    campos["perfil_lido_em"] = agora()
    sets = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(f"UPDATE wms_produtos SET {sets} WHERE stokki_id = ?", (*campos.values(), int(stokki_id)))


def produtos_sem_perfil(conn, limite: int = 200) -> list[int]:
    """Produtos ativos cujo perfil nunca foi lido ou ficou mais velho que a
    última atualização no Stokki."""
    rows = conn.execute("""
        SELECT stokki_id FROM wms_produtos
         WHERE stokki_id IS NOT NULL AND ativo = 1
           AND (perfil_lido_em IS NULL OR (stokki_atualizado_em IS NOT NULL AND stokki_atualizado_em > perfil_lido_em))
         ORDER BY perfil_lido_em IS NOT NULL, stokki_id
         LIMIT ?""", (int(limite),)).fetchall()
    return [r["stokki_id"] for r in rows]


def criar_produto_manual(conn, ean: str, descricao: str, embarcador: str = "", unidade: str = "UN",
                         qtd_por_caixa: float | None = None) -> dict:
    ean = _limpar_ean(ean)
    descricao = " ".join(str(descricao or "").split())
    if not descricao:
        raise ErroWMS("Descrição é obrigatória.")
    if ean and conn.execute("SELECT 1 FROM wms_produtos WHERE ean = ? AND ativo = 1", (ean,)).fetchone():
        raise ErroWMS(f"Já existe um produto com o EAN {ean}.")
    cur = conn.execute(
        "INSERT INTO wms_produtos (descricao, embarcador, ean, unidade, qtd_por_caixa, origem, perfil_lido_em) VALUES (?,?,?,?,?,'MANUAL',?)",
        (descricao, embarcador.strip() or None, ean, (unidade or "UN").upper()[:6], qtd_por_caixa, agora()))
    conn.commit()
    return obter_produto(conn, cur.lastrowid)


def definir_controla_validade(conn, produto_id: int, controla: bool) -> dict:
    conn.execute("UPDATE wms_produtos SET controla_validade = ?, atualizado_em = ? WHERE id = ?",
                 (1 if controla else 0, agora(), produto_id))
    conn.commit()
    return obter_produto(conn, produto_id)


def _produto_dict(row) -> dict:
    d = dict(row)
    for k in ("controla_lote", "controla_validade", "ativo"):
        d[k] = bool(d.get(k))
    return d


def obter_produto(conn, produto_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM wms_produtos WHERE id = ?", (produto_id,)).fetchone()
    return _produto_dict(row) if row else None


def buscar_por_codigo(conn, codigo: str) -> list[dict]:
    """Busca por EAN, DUN (caixa) ou SKU exato. Devolve lista (pode haver o
    mesmo EAN em mais de um embarcador) com `lido_como` em cada item."""
    bruto = str(codigo or "").strip()
    digitos = re.sub(r"\D", "", bruto)
    out = []
    if digitos and RE_EAN.match(digitos):
        candidatos = {digitos}
        # EAN-13 lido por leitor pode vir com zero à esquerda (14) ou como UPC-A (12).
        if len(digitos) == 14 and digitos.startswith("0"):
            candidatos.add(digitos[1:])
        if len(digitos) == 12:
            candidatos.add("0" + digitos)
        marks = ",".join("?" for _ in candidatos)
        for r in conn.execute(f"SELECT * FROM wms_produtos WHERE ativo = 1 AND ean IN ({marks}) ORDER BY descricao", tuple(candidatos)):
            d = _produto_dict(r); d["lido_como"] = "EAN"; out.append(d)
        for r in conn.execute(f"SELECT * FROM wms_produtos WHERE ativo = 1 AND dun IN ({marks}) ORDER BY descricao", tuple(candidatos)):
            if any(o["id"] == r["id"] for o in out):
                continue
            d = _produto_dict(r); d["lido_como"] = "DUN"; out.append(d)
    if not out and bruto:
        for r in conn.execute("SELECT * FROM wms_produtos WHERE ativo = 1 AND sku = ? COLLATE NOCASE ORDER BY descricao", (bruto,)):
            d = _produto_dict(r); d["lido_como"] = "SKU"; out.append(d)
    return out


def buscar_produtos_texto(conn, q: str, limite: int = 25) -> list[dict]:
    q = " ".join(str(q or "").split())
    if not q:
        return []
    termos = q.split()[:4]
    cond = " AND ".join("(descricao LIKE ? OR sku LIKE ? OR ean LIKE ? OR embarcador LIKE ?)" for _ in termos)
    params = []
    for t in termos:
        like = f"%{t}%"
        params += [like, like, like, like]
    rows = conn.execute(f"SELECT * FROM wms_produtos WHERE ativo = 1 AND {cond} ORDER BY descricao LIMIT ?",
                        (*params, int(limite))).fetchall()
    return [_produto_dict(r) for r in rows]


def contar_produtos(conn) -> dict:
    row = conn.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN ativo=1 THEN 1 ELSE 0 END), 0) AS ativos,
               COALESCE(SUM(CASE WHEN ativo=1 AND ean IS NOT NULL THEN 1 ELSE 0 END), 0) AS com_ean,
               COALESCE(SUM(CASE WHEN ativo=1 AND perfil_lido_em IS NOT NULL THEN 1 ELSE 0 END), 0) AS com_perfil,
               MAX(atualizado_em) AS ultima_sincronizacao
          FROM wms_produtos""").fetchone()
    return dict(row)


# ── Movimentos e saldos ──────────────────────────────────────────────────────

def _validar_validade(validade) -> str | None:
    if validade in (None, ""):
        return None
    s = str(validade).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ErroWMS(f"Validade inválida: {s!r}. Use dd/mm/aaaa.")


def _posicao_ativa(conn, codigo: str, papel: str) -> dict:
    p = obter_posicao(conn, codigo)
    if not p:
        raise ErroWMS(f"Posição de {papel} {normalizar_codigo(codigo)} não existe. Crie a posição em Posições.")
    if not p["ativo"]:
        raise ErroWMS(f"Posição {p['codigo']} está desativada.")
    return p


def _saldo_atual(conn, posicao: str, produto_id: int, lote: str, validade: str) -> float:
    row = conn.execute("SELECT quantidade FROM wms_saldos WHERE posicao=? AND produto_id=? AND lote=? AND validade=?",
                       (posicao, produto_id, lote, validade)).fetchone()
    return float(row["quantidade"]) if row else 0.0


def _gravar_saldo(conn, posicao: str, produto_id: int, lote: str, validade: str, quantidade: float) -> None:
    quantidade = round(float(quantidade), 3)
    if abs(quantidade) < 1e-9:
        conn.execute("DELETE FROM wms_saldos WHERE posicao=? AND produto_id=? AND lote=? AND validade=?",
                     (posicao, produto_id, lote, validade))
        return
    conn.execute("""
        INSERT INTO wms_saldos (posicao, produto_id, lote, validade, quantidade, atualizado_em)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(posicao, produto_id, lote, validade) DO UPDATE SET quantidade = excluded.quantidade, atualizado_em = excluded.atualizado_em
    """, (posicao, produto_id, lote, validade, quantidade, agora()))


def _aplicar_movimento_no_saldo(conn, mov: dict, permitir_negativo: bool = False) -> dict:
    """Aplica um movimento (já validado) em wms_saldos. Devolve os saldos
    resultantes por posição tocada."""
    lote, validade = mov["lote"], mov["validade"] or ""
    pid, q = mov["produto_id"], float(mov["quantidade"])
    resultado = {}
    if mov["tipo"] == "INVENTARIO":
        # quantidade = contagem absoluta na posição de destino
        _gravar_saldo(conn, mov["posicao_destino"], pid, lote, validade, q)
        resultado[mov["posicao_destino"]] = q
        return resultado
    if mov["posicao_origem"]:
        atual = _saldo_atual(conn, mov["posicao_origem"], pid, lote, validade)
        novo = atual - q
        if novo < -1e-9 and not permitir_negativo:
            raise ErroWMS(
                f"Só há {atual:g} nessa posição ({mov['posicao_origem']}) pra esse lote/validade, não dá pra tirar {q:g}.")
        _gravar_saldo(conn, mov["posicao_origem"], pid, lote, validade, novo)
        resultado[mov["posicao_origem"]] = round(novo, 3)
    if mov["posicao_destino"]:
        atual = _saldo_atual(conn, mov["posicao_destino"], pid, lote, validade)
        novo = atual + q
        _gravar_saldo(conn, mov["posicao_destino"], pid, lote, validade, novo)
        resultado[mov["posicao_destino"]] = round(novo, 3)
    return resultado


def registrar_movimento(conn, *, tipo: str, produto_id: int, quantidade, lote: str = "", validade=None,
                        origem: str | None = None, destino: str | None = None, operador: dict | None = None,
                        uuid: str | None = None, observacao: str = "", dispositivo: str = "",
                        criado_em: str | None = None, permitir_negativo: bool = False) -> dict:
    """Grava um movimento e atualiza os saldos numa transação só.
    Idempotente por uuid: se já existe, devolve o gravado com duplicado=True."""
    tipo = str(tipo or "").upper()
    if tipo not in TIPOS_MOVIMENTO:
        raise ErroWMS(f"Tipo de movimento inválido: {tipo!r}.")
    uuid = str(uuid or "").strip() or secrets.token_hex(16)
    existente = conn.execute("SELECT * FROM wms_movimentos WHERE uuid = ?", (uuid,)).fetchone()
    if existente:
        d = _movimento_dict(conn, existente); d["duplicado"] = True
        return d

    produto = obter_produto(conn, int(produto_id))
    if not produto:
        raise ErroWMS("Produto não encontrado.")
    try:
        quantidade = float(str(quantidade).replace(",", "."))
    except (TypeError, ValueError):
        raise ErroWMS("Quantidade inválida.")
    if tipo == "INVENTARIO":
        if quantidade < 0:
            raise ErroWMS("Contagem não pode ser negativa.")
    elif quantidade <= 0:
        raise ErroWMS("Quantidade precisa ser maior que zero.")
    lote = " ".join(str(lote or "").split()).upper()
    validade = _validar_validade(validade)
    origem = normalizar_codigo(origem) if origem else None
    destino = normalizar_codigo(destino) if destino else None

    if tipo == "ENTRADA":
        if not destino:
            raise ErroWMS("Entrada precisa da posição de destino.")
        origem = None
        if produto["controla_validade"] and not validade:
            raise ErroWMS("Validade é obrigatória pra esse produto.")
    elif tipo == "SAIDA":
        if not origem:
            raise ErroWMS("Saída precisa da posição de origem.")
        destino = None
    elif tipo == "TRANSFERENCIA":
        if not origem or not destino:
            raise ErroWMS("Transferência precisa de origem e destino.")
        if origem == destino:
            raise ErroWMS("Origem e destino são a mesma posição.")
    elif tipo == "AJUSTE":
        if bool(origem) == bool(destino):
            raise ErroWMS("Ajuste é pra mais (só destino) ou pra menos (só origem).")
        if not observacao.strip():
            raise ErroWMS("Ajuste precisa de um motivo.")
    elif tipo == "INVENTARIO":
        if not destino:
            raise ErroWMS("Inventário precisa da posição contada.")
        origem = None
    if origem:
        _posicao_ativa(conn, origem, "origem")
    if destino:
        _posicao_ativa(conn, destino, "destino")

    mov = {
        "uuid": uuid, "tipo": tipo, "produto_id": produto["id"], "lote": lote, "validade": validade,
        "quantidade": round(quantidade, 3), "posicao_origem": origem, "posicao_destino": destino,
        "operador_id": (operador or {}).get("id"), "operador_nome": (operador or {}).get("nome"),
        "dispositivo": (dispositivo or "")[:80], "observacao": (observacao or "").strip()[:500],
        "criado_em": criado_em or agora(),
    }
    try:
        conn.execute("BEGIN IMMEDIATE")
        saldos = _aplicar_movimento_no_saldo(conn, mov, permitir_negativo=permitir_negativo)
        cur = conn.execute("""
            INSERT INTO wms_movimentos (uuid, tipo, produto_id, lote, validade, quantidade, posicao_origem, posicao_destino,
                                        operador_id, operador_nome, dispositivo, observacao, criado_em)
            VALUES (:uuid, :tipo, :produto_id, :lote, :validade, :quantidade, :posicao_origem, :posicao_destino,
                    :operador_id, :operador_nome, :dispositivo, :observacao, :criado_em)""", mov)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    d = _movimento_dict(conn, conn.execute("SELECT * FROM wms_movimentos WHERE id = ?", (cur.lastrowid,)).fetchone())
    d["saldos"] = saldos
    d["duplicado"] = False
    return d


def _movimento_dict(conn, row) -> dict:
    d = dict(row)
    p = conn.execute("SELECT descricao, sku, ean, embarcador, unidade, qtd_por_caixa FROM wms_produtos WHERE id = ?",
                     (d["produto_id"],)).fetchone()
    d["produto"] = dict(p) if p else None
    return d


def listar_movimentos(conn, limite: int = 50, produto_id: int | None = None, posicao: str | None = None,
                      operador_id: int | None = None, desde: str | None = None) -> list[dict]:
    cond, params = [], []
    if produto_id:
        cond.append("m.produto_id = ?"); params.append(int(produto_id))
    if posicao:
        posicao = normalizar_codigo(posicao)
        cond.append("(m.posicao_origem = ? OR m.posicao_destino = ?)"); params += [posicao, posicao]
    if operador_id:
        cond.append("m.operador_id = ?"); params.append(int(operador_id))
    if desde:
        cond.append("m.criado_em >= ?"); params.append(desde)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    rows = conn.execute(f"""
        SELECT m.*, p.descricao AS produto_descricao, p.sku AS produto_sku, p.embarcador AS produto_embarcador,
               p.unidade AS produto_unidade, p.qtd_por_caixa AS produto_qtd_por_caixa
          FROM wms_movimentos m JOIN wms_produtos p ON p.id = m.produto_id
          {where}
         ORDER BY m.id DESC LIMIT ?""", (*params, int(limite))).fetchall()
    return [dict(r) for r in rows]


def _dias_para_vencer(validade: str) -> int | None:
    if not validade:
        return None
    try:
        return (date.fromisoformat(validade) - date.today()).days
    except ValueError:
        return None


def saldos_produto(conn, produto_id: int) -> list[dict]:
    rows = conn.execute("""
        SELECT s.*, p.area_codigo, a.nome AS area_nome, a.temperatura, p.ativo AS posicao_ativa
          FROM wms_saldos s
          JOIN wms_posicoes p ON p.codigo = s.posicao
          JOIN wms_areas a ON a.codigo = p.area_codigo
         WHERE s.produto_id = ? AND s.quantidade <> 0
         ORDER BY s.validade = '' , s.validade, s.posicao""", (int(produto_id),)).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["validade"] = d["validade"] or None
        d["dias_para_vencer"] = _dias_para_vencer(d["validade"]); out.append(d)
    return out


def conteudo_posicao(conn, codigo: str) -> list[dict]:
    rows = conn.execute("""
        SELECT s.*, pr.descricao, pr.sku, pr.ean, pr.embarcador, pr.unidade, pr.qtd_por_caixa
          FROM wms_saldos s JOIN wms_produtos pr ON pr.id = s.produto_id
         WHERE s.posicao = ? AND s.quantidade <> 0
         ORDER BY pr.descricao, s.validade""", (normalizar_codigo(codigo),)).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["validade"] = d["validade"] or None
        d["dias_para_vencer"] = _dias_para_vencer(d["validade"]); out.append(d)
    return out


def ler_codigo(conn, codigo: str) -> dict:
    """Decide o que um código lido representa: posição (QR "POS:" ou padrão
    de endereço), produto (EAN/DUN/SKU) ou desconhecido."""
    bruto = str(codigo or "").strip()
    if not bruto:
        return {"tipo": "vazio"}
    if bruto.upper().startswith(PREFIXO_QR) or parece_posicao(bruto):
        try:
            p = parse_posicao(bruto)
        except ErroWMS as e:
            return {"tipo": "invalido", "erro": str(e)}
        pos = obter_posicao(conn, p["codigo"])
        if not pos:
            return {"tipo": "posicao_inexistente", "codigo": p["codigo"], "parse": p}
        return {"tipo": "posicao", "posicao": pos, "conteudo": conteudo_posicao(conn, p["codigo"])}
    produtos = buscar_por_codigo(conn, bruto)
    if not produtos:
        return {"tipo": "desconhecido", "codigo": bruto,
                "parece_ean": bool(RE_EAN.match(re.sub(r"\D", "", bruto)))}
    for p in produtos:
        p["saldos"] = saldos_produto(conn, p["id"])
        p["total"] = round(sum(s["quantidade"] for s in p["saldos"]), 3)
    return {"tipo": "produto", "produtos": produtos, "codigo": bruto}


def resumo_dia(conn, dia: date | None = None) -> dict:
    dia = dia or date.today()
    ini = dia.strftime("%Y-%m-%d 00:00:00")
    row = conn.execute("""
        SELECT COUNT(*) AS movimentos, COUNT(DISTINCT operador_id) AS operadores,
               COALESCE(SUM(CASE WHEN tipo='ENTRADA' THEN 1 ELSE 0 END), 0) AS entradas,
               COALESCE(SUM(CASE WHEN tipo='SAIDA' THEN 1 ELSE 0 END), 0) AS saidas,
               COALESCE(SUM(CASE WHEN tipo='TRANSFERENCIA' THEN 1 ELSE 0 END), 0) AS transferencias
          FROM wms_movimentos WHERE criado_em >= ?""", (ini,)).fetchone()
    pos = conn.execute("""
        SELECT COUNT(*) AS posicoes, SUM(CASE WHEN ocupada THEN 1 ELSE 0 END) AS ocupadas FROM (
            SELECT p.codigo, EXISTS(SELECT 1 FROM wms_saldos s WHERE s.posicao = p.codigo AND s.quantidade > 0) AS ocupada
              FROM wms_posicoes p WHERE p.ativo = 1)""").fetchone()
    venc = conn.execute("""
        SELECT COUNT(*) AS n FROM wms_saldos WHERE quantidade > 0 AND validade <> '' AND validade <= ?""",
                        ((dia + timedelta(days=7)).isoformat(),)).fetchone()
    d = dict(row); d.update(dict(pos)); d["vencendo_7d"] = venc["n"]
    return d


def recalcular_saldos(conn) -> int:
    """Reconstrói wms_saldos a partir de wms_movimentos (ordem de id).
    Uso administrativo; devolve o nº de linhas de saldo resultantes."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM wms_saldos")
        for row in conn.execute("SELECT * FROM wms_movimentos ORDER BY id").fetchall():
            _aplicar_movimento_no_saldo(conn, dict(row), permitir_negativo=True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute("SELECT COUNT(*) AS n FROM wms_saldos").fetchone()["n"]


# ── Etiquetas (PDF 10 × 5 cm) ────────────────────────────────────────────────

_DPI = 300
_LARGURA_PX, _ALTURA_PX = int(10 / 2.54 * _DPI), int(5 / 2.54 * _DPI)  # 1181 × 591


def _fonte(tamanho: int, negrito: bool = True, condensada: bool = False):
    from PIL import ImageFont
    if condensada:
        candidatos = ["C:/Windows/Fonts/ARIALNB.TTF", "C:/Windows/Fonts/arialnb.ttf",
                      "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
                      "/usr/share/fonts/truetype/liberation2/LiberationSansNarrow-Bold.ttf"]
    else:
        candidatos = []
    candidatos += (["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if negrito else
                   ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for caminho in candidatos:
        try:
            return ImageFont.truetype(caminho, tamanho)
        except OSError:
            continue
    return ImageFont.load_default()


def _qr_imagem(conteudo: str, lado_px: int):
    import qrcode
    from qrcode.constants import ERROR_CORRECT_H
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=10, border=1)
    qr.add_data(conteudo)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("L")
    return img.resize((lado_px, lado_px))


def _ajustar_fonte(draw, texto: str, largura_max: int, tamanho_inicial: int, **kw):
    tamanho = tamanho_inicial
    while tamanho > 20:
        f = _fonte(tamanho, **kw)
        if draw.textlength(texto, font=f) <= largura_max:
            return f
        tamanho -= 6
    return _fonte(tamanho, **kw)


def desenhar_etiqueta(posicao: dict):
    """Devolve uma imagem PIL (modo L, 1181×591 @300dpi) da etiqueta da posição."""
    from PIL import Image, ImageDraw
    W, H = _LARGURA_PX, _ALTURA_PX
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)
    # faixa lateral preta com o nome da área (vertical)
    faixa = 120
    draw.rectangle([0, 0, faixa, H], fill=0)
    nome_area = (posicao.get("area_nome") or posicao["area_codigo"]).upper()
    f_area = _ajustar_fonte(draw, nome_area, H - 40, 64, condensada=True)
    tw = draw.textlength(nome_area, font=f_area)
    faixa_img = Image.new("L", (int(tw) + 20, faixa), 0)
    ImageDraw.Draw(faixa_img).text((10, (faixa - f_area.size) / 2 - 6), nome_area, fill=255, font=f_area)
    faixa_img = faixa_img.rotate(90, expand=True)
    img.paste(faixa_img, (0, (H - faixa_img.height) // 2))
    # QR
    lado = H - 70
    qr = _qr_imagem(PREFIXO_QR + posicao["codigo"], lado)
    img.paste(qr, (faixa + 30, 35))
    # textos
    x0 = faixa + 30 + lado + 40
    largura_texto = W - x0 - 30
    f_codigo = _ajustar_fonte(draw, posicao["codigo"], largura_texto, 190, condensada=True)
    draw.text((x0, 60), posicao["codigo"], fill=0, font=f_codigo)
    f_desc = _ajustar_fonte(draw, posicao["descricao"], largura_texto, 60, negrito=True)
    draw.text((x0, 60 + f_codigo.size + 30), posicao["descricao"], fill=0, font=f_desc)
    rodape = f"{PREFIXO_QR}{posicao['codigo']}  ·  {(posicao.get('temperatura') or '').capitalize()}  ·  Freshlog"
    f_rod = _ajustar_fonte(draw, rodape, largura_texto, 30, negrito=False)
    draw.text((x0, H - 75), rodape, fill=0, font=f_rod)
    # borda de corte
    draw.rectangle([0, 0, W - 1, H - 1], outline=0, width=2)
    return img


ORIENTACOES_ETIQUETA = ("retrato", "retrato-inv", "paisagem")


def _aplicar_margem(img, margem_mm: float, desloc_x_mm: float, desloc_y_mm: float):
    """Encolhe a arte dentro da página (margem branca igual nos 4 lados) e a
    desloca em mm -- ajuste fino pra impressora térmica sem mexer no driver."""
    from PIL import Image
    if not margem_mm and not desloc_x_mm and not desloc_y_mm:
        return img
    px_mm = _DPI / 25.4
    W, H = img.size
    m = max(0, int(round(margem_mm * px_mm)))
    arte = img.resize((max(1, W - 2 * m), max(1, H - 2 * m)))
    pagina = Image.new("L", (W, H), 255)
    pagina.paste(arte, (m + int(round(desloc_x_mm * px_mm)), m + int(round(desloc_y_mm * px_mm))))
    return pagina


def gerar_etiquetas_pdf(conn, codigos: list[str], orientacao: str = "retrato", margem_mm: float = 0,
                        desloc_x_mm: float = 0, desloc_y_mm: float = 0) -> bytes:
    """PDF com uma etiqueta por página, no tamanho exato da mídia térmica.

    orientacao:
      retrato      (padrão) página 5 × 10 cm em pé, arte girada 90° no sentido
                   horário -- é como a impressora do galpão puxa a etiqueta
                   (foto do Hugo, 04/09: a faixa da área sai no topo). Imprimir
                   em 100% / "tamanho real", sem "ajustar à página".
      retrato-inv  mesma página em pé, girada pro outro lado (se a impressora
                   puxar a etiqueta pelo outro lado).
      paisagem     página 10 × 5 cm deitada, arte sem girar.
    """
    if orientacao not in ORIENTACOES_ETIQUETA:
        raise ErroWMS(f"Orientação inválida: {orientacao!r}. Use retrato, retrato-inv ou paisagem.")
    posicoes = []
    for c in codigos:
        p = obter_posicao(conn, c)
        if not p:
            raise ErroWMS(f"Posição {normalizar_codigo(c)} não existe.")
        posicoes.append(p)
    if not posicoes:
        raise ErroWMS("Nenhuma posição pra imprimir.")
    imagens = [desenhar_etiqueta(p) for p in posicoes]
    if orientacao == "retrato":
        imagens = [im.rotate(-90, expand=True) for im in imagens]
    elif orientacao == "retrato-inv":
        imagens = [im.rotate(90, expand=True) for im in imagens]
    if margem_mm < 0 or margem_mm > 15 or abs(desloc_x_mm) > 15 or abs(desloc_y_mm) > 15:
        raise ErroWMS("Margem e deslocamento devem ficar entre 0 e 15 mm.")
    imagens = [_aplicar_margem(im, margem_mm, desloc_x_mm, desloc_y_mm) for im in imagens]
    buf = io.BytesIO()
    imagens[0].save(buf, "PDF", resolution=float(_DPI), save_all=True, append_images=imagens[1:],
                    title="Etiquetas de posição Freshlog")
    return buf.getvalue()


# ── CLI administrativo ───────────────────────────────────────────────────────

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Administração do endereçamento (WMS).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("semear", help="cria as áreas e posições iniciais (idempotente)")
    p_op = sub.add_parser("operador", help="cria/redefine PIN de um operador")
    p_op.add_argument("nome"); p_op.add_argument("pin")
    sub.add_parser("operadores", help="lista operadores")
    p_et = sub.add_parser("etiquetas", help="gera PDF de etiquetas de uma área")
    p_et.add_argument("area"); p_et.add_argument("saida")
    sub.add_parser("recalcular", help="reconstrói wms_saldos a partir dos movimentos")
    sub.add_parser("resumo", help="contagens gerais")
    args = ap.parse_args(argv)
    conn = conectar()
    if args.cmd == "semear":
        print(semear_estrutura_inicial(conn))
    elif args.cmd == "operador":
        print(criar_operador(conn, args.nome, args.pin))
    elif args.cmd == "operadores":
        for o in listar_operadores(conn, apenas_ativos=False):
            print(o)
    elif args.cmd == "etiquetas":
        codigos = [p["codigo"] for p in listar_posicoes(conn, args.area)]
        Path(args.saida).write_bytes(gerar_etiquetas_pdf(conn, codigos))
        print(f"{len(codigos)} etiquetas em {args.saida}")
    elif args.cmd == "recalcular":
        print("linhas de saldo:", recalcular_saldos(conn))
    elif args.cmd == "resumo":
        print("areas:", len(listar_areas(conn)), "posicoes:", len(listar_posicoes(conn)))
        print("produtos:", contar_produtos(conn))
        print("hoje:", resumo_dia(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())