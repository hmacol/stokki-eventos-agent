# -*- coding: utf-8 -*-
"""
Pedidos dedicados (Hugo, 23/09/2026): marcacao de pedido que vai em
transporte dedicado (cotado a parte), com o valor da cotacao dividido
entre os pedidos do mesmo grupo. Alimenta:
  - roteirizacao (criar_rotas_diarias / incrementar_rotas): dedicado fica
    fora da rota compartilhada;
  - planejamento (pool): chip "Dedicado - R$ X";
  - portal do cliente (aba Envios): chip "Envio dedicado";
  - notificar_dedicados_financeiro.py: e-mail da quinzena (dias 1 e 16).

Tabela pedidos_dedicados, uma linha por pedido. Chave logica: codigo_pedido
(PS-NNNNN) OU envio_id (portal_envios.id) enquanto o codigo nao existe.
Remocao e logica (removido_em), pra quinzena fechada nao mudar.
"""
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent
DB_PATH = _RAIZ / "dados" / "dados.db"

_PADRAO_PS = re.compile(r"^#?\s*PS\s*[-._ ]?\s*(\d{1,7})", re.IGNORECASE)
_CAMPOS_IDENTIDADE = ("codigo_pedido", "service_id", "envio_id", "sender_id", "remetente_nome", "numero_nf")


def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pedidos_dedicados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo_pedido TEXT,
            service_id INTEGER,
            envio_id INTEGER,
            sender_id INTEGER,
            remetente_nome TEXT,
            numero_nf TEXT,
            valor REAL NOT NULL,
            grupo_id TEXT NOT NULL,
            valor_total_grupo REAL NOT NULL,
            marcado_em TEXT NOT NULL,
            marcado_por TEXT,
            removido_em TEXT,
            removido_por TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dedicados_codigo ON pedidos_dedicados (codigo_pedido, removido_em);
        CREATE INDEX IF NOT EXISTS idx_dedicados_envio ON pedidos_dedicados (envio_id, removido_em);
        CREATE INDEX IF NOT EXISTS idx_dedicados_marcado ON pedidos_dedicados (marcado_em);
    """)
    conn.commit()
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalizar_codigo(codigo) -> str:
    """'#ps-1234-R1' / 'PS 1234' / 'PS1234' -> 'PS-1234'. Sem o padrao PS,
    devolve o texto sem '#', maiusculo (pode ser NF do cliente)."""
    texto = str(codigo or "").strip()
    m = _PADRAO_PS.match(texto)
    if m:
        return f"PS-{int(m.group(1))}"
    return texto.lstrip("#").strip().upper()


def codigos_do_servico(servico: dict) -> list[str]:
    """service.code da Vuupt pode trazer mais de um pedido: '#PS-1, PS-2'."""
    bruto = str((servico or {}).get("code") or "")
    return [normalizar_codigo(p) for p in bruto.split(",") if p.strip()]


def dividir_valor(total: float, n: int) -> list[float]:
    """Divide em centavos; a sobra vai pro primeiro. Soma bate com o total."""
    if n <= 0:
        raise ValueError("n precisa ser >= 1")
    if total < 0:
        raise ValueError("valor negativo")
    centavos = int(round(float(total) * 100))
    base, sobra = divmod(centavos, n)
    partes = [base + sobra] + [base] * (n - 1)
    return [p / 100 for p in partes]


def _ativo(conn, *, codigo_pedido=None, envio_id=None) -> sqlite3.Row | None:
    if codigo_pedido:
        r = conn.execute("SELECT * FROM pedidos_dedicados WHERE codigo_pedido = ? AND removido_em IS NULL ORDER BY id DESC LIMIT 1",
                         (codigo_pedido,)).fetchone()
        if r:
            return r
    if envio_id is not None:
        return conn.execute("SELECT * FROM pedidos_dedicados WHERE envio_id = ? AND removido_em IS NULL ORDER BY id DESC LIMIT 1",
                            (envio_id,)).fetchone()
    return None


def marcar(conn: sqlite3.Connection, pedidos: list[dict], valor_total: float, por: str) -> str:
    """Cada pedido: {codigo_pedido?, service_id?, envio_id?, sender_id?,
    remetente_nome?, numero_nf?}. Pedido ja ativo (mesmo codigo ou mesmo
    envio_id) e atualizado, nao duplicado. Devolve o grupo_id."""
    if not pedidos:
        raise ValueError("nenhum pedido")
    partes = dividir_valor(valor_total, len(pedidos))
    grupo = uuid.uuid4().hex[:12]
    agora = _agora()
    for p, valor in zip(pedidos, partes):
        codigo = normalizar_codigo(p.get("codigo_pedido")) or None
        envio_id = p.get("envio_id")
        campos = {
            "codigo_pedido": codigo, "service_id": p.get("service_id"), "envio_id": envio_id,
            "sender_id": p.get("sender_id"), "remetente_nome": p.get("remetente_nome"), "numero_nf": p.get("numero_nf"),
            "valor": valor, "grupo_id": grupo, "valor_total_grupo": float(valor_total),
            "marcado_em": agora, "marcado_por": por,
        }
        existente = _ativo(conn, codigo_pedido=codigo, envio_id=envio_id)
        if existente:
            # identidade so preenche o que estava vazio; valor/grupo/quem marcou
            # sao trocados; marcado_em fica o ORIGINAL (remarcar pra corrigir
            # valor nao pode mudar a quinzena do financeiro -- revisao 23/09)
            campos_upd = {k: v for k, v in campos.items() if k != "marcado_em"}
            sets = ", ".join(f"{k} = COALESCE(?, {k})" if k in _CAMPOS_IDENTIDADE else f"{k} = ?" for k in campos_upd)
            conn.execute(f"UPDATE pedidos_dedicados SET {sets} WHERE id = ?", (*campos_upd.values(), existente["id"]))
        else:
            cols = ", ".join(campos)
            conn.execute(f"INSERT INTO pedidos_dedicados ({cols}) VALUES ({','.join('?' * len(campos))})", tuple(campos.values()))
    conn.commit()
    return grupo


def remover(conn: sqlite3.Connection, *, codigo_pedido=None, envio_id=None, service_id=None, por: str = "") -> int:
    cond, params = [], []
    if codigo_pedido:
        cond.append("codigo_pedido = ?")
        params.append(normalizar_codigo(codigo_pedido))
    if envio_id is not None:
        cond.append("envio_id = ?")
        params.append(envio_id)
    if service_id is not None:
        cond.append("service_id = ?")
        params.append(service_id)
    if not cond:
        return 0
    cur = conn.execute(f"UPDATE pedidos_dedicados SET removido_em = ?, removido_por = ? "
                       f"WHERE removido_em IS NULL AND ({' OR '.join(cond)})", (_agora(), por, *params))
    conn.commit()
    return cur.rowcount


def ativos_por_codigo(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM pedidos_dedicados WHERE removido_em IS NULL AND codigo_pedido IS NOT NULL ORDER BY id").fetchall()
    return {r["codigo_pedido"]: dict(r) for r in rows}


def ativo_por_envio(conn: sqlite3.Connection, envio_id: int) -> dict | None:
    r = _ativo(conn, envio_id=envio_id)
    return dict(r) if r else None


def vincular_codigo(conn: sqlite3.Connection, envio_id: int, codigo_pedido: str) -> None:
    """Chamado pela reconciliacao do worker quando descobre o PS do envio."""
    conn.execute("UPDATE pedidos_dedicados SET codigo_pedido = ? WHERE envio_id = ? AND removido_em IS NULL AND codigo_pedido IS NULL",
                 (normalizar_codigo(codigo_pedido), envio_id))
    conn.commit()


def filtrar_dedicados(servicos: list[dict], ativos: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Separa servicos Vuupt: (fora da marca, dedicados). Servico com mais
    de um codigo sai inteiro se qualquer um for dedicado (conservador)."""
    restantes, dedicados = [], []
    for s in servicos:
        if any(c in ativos for c in codigos_do_servico(s)):
            dedicados.append(s)
        else:
            restantes.append(s)
    return restantes, dedicados


def quinzena_anterior(data_ref: date) -> tuple[date, date]:
    """Dia 16 em diante -> 1..15 do mes. Dia 1 a 15 -> 16..ultimo do mes
    anterior. Ou seja: a ultima quinzena fechada antes de data_ref."""
    if data_ref.day >= 16:
        return date(data_ref.year, data_ref.month, 1), date(data_ref.year, data_ref.month, 15)
    ultimo_mes_anterior = date(data_ref.year, data_ref.month, 1) - timedelta(days=1)
    return date(ultimo_mes_anterior.year, ultimo_mes_anterior.month, 16), ultimo_mes_anterior


def listar_quinzena(conn: sqlite3.Connection, data_ref: date) -> tuple[date, date, list[dict]]:
    ini, fim = quinzena_anterior(data_ref)
    rows = conn.execute("SELECT * FROM pedidos_dedicados WHERE removido_em IS NULL AND marcado_em >= ? AND marcado_em < ? "
                        "ORDER BY remetente_nome, marcado_em, id",
                        (f"{ini.isoformat()} 00:00:00", f"{(fim + timedelta(days=1)).isoformat()} 00:00:00")).fetchall()
    return ini, fim, [dict(r) for r in rows]
