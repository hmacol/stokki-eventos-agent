# -*- coding: utf-8 -*-
"""
ciencia_dedicado.py

Registro da ciencia obrigatoria do dedicado fora do dia fixo (spec
docs/superpowers/specs/2026-11-17-ciencia-dedicado-dia-fixo-design.md, secao 1).

Duas tabelas em dados/dados.db:
  ciencias_dedicado         uma linha por pedido marcado por fora_dia_fixo; um codigo
                            pode ter varias linhas ao longo do tempo, so uma PENDENTE
  ciencias_dedicado_envios  uma linha por tentativa de envio, por canal

Nada e apagado: e o historico que responde a contestacao. Datas em texto
'YYYY-MM-DD HH:MM:SS' (como registro_dia_fixo._quando).
"""
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent
for _p in (_RAIZ, _RAIZ / "roteirizacao"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from nucleo.normalizacao import normalizar_codigo  # noqa: E402  (mantem -R/-C, ao contrario de pedidos_dedicados)
import regioes_dia_fixo  # noqa: E402
import pedidos_dedicados  # noqa: E402

DB_PATH = _RAIZ / "dados" / "dados.db"

PENDENTE = "PENDENTE"
CIENTE = "CIENTE"
REAGENDADO = "REAGENDADO"
ENCERRADO = "ENCERRADO"

# removido_por do dedicado quando o embarcador reagenda pela pagina de ciencia
POR_REAGENDADO = pedidos_dedicados.POR_CIENCIA_REAGENDADO


def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ciencias_dedicado (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT NOT NULL,
            service_id INTEGER,
            sender_id INTEGER,
            cnpj TEXT,
            regiao TEXT,
            regra_nome TEXT,
            dias_visita TEXT,
            destinatario TEXT,
            cidade TEXT,
            data_entrega TEXT,
            valor REAL,
            valor_pendente INTEGER NOT NULL DEFAULT 0,
            criado_em TEXT NOT NULL,
            situacao TEXT NOT NULL DEFAULT 'PENDENTE',
            motivo_encerramento TEXT,
            respondido_em TEXT,
            respondido_por TEXT,
            respondido_ip TEXT,
            data_nova TEXT,
            proximo_lembrete_em TEXT,
            alerta_equipe_em TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ciencias_dedicado_codigo ON ciencias_dedicado (codigo, situacao);
        CREATE INDEX IF NOT EXISTS idx_ciencias_dedicado_sender ON ciencias_dedicado (sender_id, situacao);
        CREATE TABLE IF NOT EXISTS ciencias_dedicado_envios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ciencia_id INTEGER NOT NULL,
            codigo TEXT NOT NULL,
            canal TEXT NOT NULL,
            enviado_em TEXT NOT NULL,
            destino TEXT,
            resultado TEXT NOT NULL,
            motivo TEXT,
            rodada INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ciencias_dedicado_envios ON ciencias_dedicado_envios (ciencia_id);
    """)
    conn.commit()
    return conn


def _quando(agora: datetime | None) -> str:
    return (agora or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _hhmm(valor, padrao: time) -> time:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(valor or ""))
    if not m:
        return padrao
    try:
        return time(int(m.group(1)), int(m.group(2)))
    except ValueError:
        return padrao


def cfg(config: dict | None) -> dict:
    """Secao ciencia_dedicado do config ja carregado; tolera secao ausente."""
    c = (config or {}).get("ciencia_dedicado") or {}
    janela = c.get("janela") or {}
    return {
        "ativo": bool(c.get("ativo", False)),
        "intervalo_horas": int(c.get("intervalo_horas") or 4),
        "janela": (_hhmm(janela.get("inicio"), time(8, 0)), _hhmm(janela.get("fim"), time(20, 0))),
        "forcar_destino": str(c.get("forcar_destino") or ""),
    }


def _destinatario(servico: dict) -> str:
    nome = (servico.get("customer") or {}).get("name")
    if nome:
        return str(nome)
    # title '#PS-1 - ref / EMBARCADOR / CLIENTE': o cliente e o ultimo segmento
    partes = [p.strip() for p in str(servico.get("title") or "").split("/") if p.strip()]
    return partes[-1] if partes else ""


def abrir(conn, item: dict, cnpj: str | None, agora: datetime) -> int | None:
    """Abre ciencia PENDENTE (lembrete imediato). None se ja ha PENDENTE do codigo."""
    servico = item["servico"]
    regra = item["regra"]
    # service.code pode trazer varios pedidos ('#PS-1, PS-2'): vale o primeiro
    codigo = normalizar_codigo(str(servico.get("code") or "").split(",")[0])
    if not codigo:
        return None
    if conn.execute("SELECT 1 FROM ciencias_dedicado WHERE codigo = ? AND situacao = ?",
                    (codigo, PENDENTE)).fetchone():
        return None
    quando = _quando(agora)
    cur = conn.execute("""
        INSERT INTO ciencias_dedicado (codigo, service_id, sender_id, cnpj, regiao, regra_nome,
            dias_visita, destinatario, cidade, data_entrega, valor, valor_pendente, criado_em,
            situacao, proximo_lembrete_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (codigo, servico.get("id"), servico.get("sender_id"), cnpj,
          regra.get("regiao") or regra["nome"], regra["nome"],
          regioes_dia_fixo.descricao_dias(regra), _destinatario(servico),
          regioes_dia_fixo.extrair_cidade(servico) or "",
          item["data"].isoformat(), item.get("valor"), 1 if item.get("valor_pendente") else 0,
          quando, PENDENTE, quando))
    conn.commit()
    return cur.lastrowid


def existe_para_servico(conn, servico: dict) -> bool:
    """True se ha qualquer ciencia (de qualquer situacao) do servico, pelo codigo
    (mesma normalizacao do abrir) ou pelo service_id."""
    codigo = normalizar_codigo(str(servico.get("code") or "").split(",")[0])
    return conn.execute("SELECT 1 FROM ciencias_dedicado WHERE codigo = ? OR (service_id IS NOT NULL AND service_id = ?)",
                        (codigo, servico.get("id"))).fetchone() is not None


def _dicts(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def buscar(conn, ciencia_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM ciencias_dedicado WHERE id = ?", (ciencia_id,)).fetchone()
    return dict(r) if r else None


def pendentes_vencidos(conn, agora: datetime) -> list[dict]:
    return _dicts(conn.execute(
        "SELECT * FROM ciencias_dedicado WHERE situacao = ? AND proximo_lembrete_em <= ? ORDER BY id",
        (PENDENTE, _quando(agora))))


def pendentes_do_sender(conn, sender_id) -> list[dict]:
    return _dicts(conn.execute(
        "SELECT * FROM ciencias_dedicado WHERE situacao = ? AND sender_id = ? ORDER BY id",
        (PENDENTE, sender_id)))


def do_sender(conn, sender_id, dias: int = 30, agora: datetime | None = None) -> list[dict]:
    """Ciencias do embarcador: todas as PENDENTE e as respondidas/encerradas nos
    ultimos `dias` dias (pela data da resposta), mais recentes primeiro."""
    corte = _quando((agora or datetime.now()) - timedelta(days=dias))
    return _dicts(conn.execute(
        "SELECT * FROM ciencias_dedicado WHERE sender_id = ? AND (situacao = ? OR respondido_em >= ?) "
        "ORDER BY id DESC", (sender_id, PENDENTE, corte)))


def listar(conn, situacao=None, sender_id=None, limite=300) -> list[dict]:
    sql, args, filtros = "SELECT * FROM ciencias_dedicado", [], []
    if situacao:
        filtros.append("situacao = ?")
        args.append(situacao)
    if sender_id is not None:
        filtros.append("sender_id = ?")
        args.append(sender_id)
    if filtros:
        sql += " WHERE " + " AND ".join(filtros)
    return _dicts(conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limite)))


def registrar_envio(conn, ciencia_id, canal, resultado, destino="", motivo="", rodada=0, agora=None):
    row = conn.execute("SELECT codigo FROM ciencias_dedicado WHERE id = ?", (ciencia_id,)).fetchone()
    conn.execute("""
        INSERT INTO ciencias_dedicado_envios (ciencia_id, codigo, canal, enviado_em, destino,
            resultado, motivo, rodada) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (ciencia_id, row["codigo"] if row else "", canal, _quando(agora), destino, resultado,
          motivo, rodada))
    conn.commit()


def envios(conn, ciencia_id) -> list[dict]:
    return _dicts(conn.execute(
        "SELECT * FROM ciencias_dedicado_envios WHERE ciencia_id = ? ORDER BY id", (ciencia_id,)))


# uma linha por rodada (enviado_em) do pedido; `cadastro` = nao_enviado que nao e desligado nem falha
_RODADAS_SQL = """
    SELECT SUM(resultado = 'enviado') AS reais, SUM(resultado = 'piloto') AS pilotos,
           SUM(resultado = 'nao_enviado' AND COALESCE(motivo, '') NOT IN ('desligado', 'falha')) AS cadastro
    FROM ciencias_dedicado_envios WHERE ciencia_id = ? GROUP BY enviado_em
"""


def rodadas_enviadas(conn, ciencia_id) -> int:
    """Numero da proxima rodada: quantas rodadas ja aconteceram fora do piloto.

    Uma rodada e uma chamada de envio do embarcador; todos os envios dela gravam
    o mesmo enviado_em, que e o que identifica a rodada (o numero gravado em
    `rodada` se repete enquanto o piloto nao conta). Conta a rodada com algum
    envio real ('enviado') e tambem a que so teve 'nao_enviado' por falta de
    cadastro ou teto (embarcador sem canal: senao ficaria na rodada 0 pra sempre
    e alertaria a equipe a cada rodada). Nao contam: rodada de piloto (alguma
    copia 'piloto' e nenhum 'enviado', mesmo com um canal 'nao_enviado'), pra a
    virada de piloto pra envio real recomecar na rodada 0; e rodada so de
    'nao_enviado' por 'desligado' ou 'falha', pra a primeira mensagem que o
    cliente de fato recebe nao sair como lembrete."""
    return conn.execute(f"""
        SELECT COUNT(*) FROM ({_RODADAS_SQL})
        WHERE reais > 0 OR (pilotos = 0 AND cadastro > 0)
    """, (ciencia_id,)).fetchone()[0]


def rodadas_piloto(conn, ciencia_id) -> int:
    """Quantas rodadas de piloto ja aconteceram (alguma copia 'piloto', nenhum
    'enviado'). So escolhe o texto das copias de piloto (lembrete ou nao): o
    numero da rodada continua sendo rodadas_enviadas."""
    return conn.execute(f"""
        SELECT COUNT(*) FROM ({_RODADAS_SQL}) WHERE reais = 0 AND pilotos > 0
    """, (ciencia_id,)).fetchone()[0]


def agendar_proximo(conn, ciencia_id, agora: datetime, intervalo_horas: int):
    conn.execute("UPDATE ciencias_dedicado SET proximo_lembrete_em = ? WHERE id = ?",
                 (_quando(agora + timedelta(hours=intervalo_horas)), ciencia_id))
    conn.commit()


def responder(conn, ciencia_id, situacao, nome, ip, agora: datetime, data_nova: date | None = None) -> bool:
    """Registra a resposta do embarcador. So muda linha PENDENTE."""
    cur = conn.execute("""
        UPDATE ciencias_dedicado SET situacao = ?, respondido_em = ?, respondido_por = ?,
            respondido_ip = ?, data_nova = ?
        WHERE id = ? AND situacao = ?
    """, (situacao, _quando(agora), nome, ip, data_nova.isoformat() if data_nova else None,
          ciencia_id, PENDENTE))
    conn.commit()
    return cur.rowcount > 0


def encerrar(conn, ciencia_id, motivo, agora: datetime) -> bool:
    """Para os lembretes sem resposta (pedido entregue, cancelado, dedicado removido...)."""
    cur = conn.execute("""
        UPDATE ciencias_dedicado SET situacao = ?, motivo_encerramento = ?, respondido_em = ?
        WHERE id = ? AND situacao = ?
    """, (ENCERRADO, motivo, _quando(agora), ciencia_id, PENDENTE))
    conn.commit()
    return cur.rowcount > 0


def atualizar_data(conn, ciencia_id, data: date):
    conn.execute("UPDATE ciencias_dedicado SET data_entrega = ? WHERE id = ?",
                 (data.isoformat(), ciencia_id))
    conn.commit()


def marcar_alerta_equipe(conn, ciencia_id, agora: datetime):
    conn.execute("UPDATE ciencias_dedicado SET alerta_equipe_em = ? WHERE id = ?",
                 (_quando(agora), ciencia_id))
    conn.commit()


def proximo_envio_permitido(agora: datetime, janela) -> datetime:
    """Dentro da janela devolve `agora`; antes dela, o inicio do dia; depois, o inicio do dia seguinte."""
    inicio, fim = janela
    if agora.time() < inicio:
        return datetime.combine(agora.date(), inicio)
    if agora.time() > fim:
        return datetime.combine(agora.date() + timedelta(days=1), inicio)
    return agora
