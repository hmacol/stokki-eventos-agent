# -*- coding: utf-8 -*-
"""
tratativas.py

Histórico de tratativas por pedido -- pedido do Hugo, 14/08: "histórico de
tratativas com possibilidade de consulta por NF, PS, nome do embarcador,
nome do cliente ou qualquer informação do pedido... filtrar motorista,
motivo da falha e acesso pra ler a tratativa".

Levantamento (14/08) mostrou que não existia nada assim: `ocorrencias_pedido`
é um protótipo órfão (schema rico, mas nenhum código escreve nela e os
campos de NF/embarcador/resposta estão vazios); o fluxo que roda de verdade
em produção (insucesso_entrega/) e a Torre de Controle guardam pedaços
soltos, sem busca. Este módulo centraliza um LOG DE EVENTOS append-only
(tratativas_pedido) alimentado nos pontos onde essas tratativas já
acontecem hoje -- ver pontos de chamada em expedir_pedidos.py,
notificar_insucesso_aguardando_resposta.py, ler_respostas_insucesso.py e
painel_agentes/torre_controle.py.

NF, embarcador e destinatário NÃO são duplicados aqui -- já existem e são
confiáveis em `pedidos_historico` (alimentada pelo pipeline Stokki→VUUPT),
então `buscar()` faz LEFT JOIN por `pedido_code` na hora da consulta, sempre
com o dado mais fresco.

Motorista é a exceção: não tem histórico persistente em lugar nenhum hoje
(só é resolvido "ao vivo", por ROTA, nunca por serviço isolado -- pedido do
Hugo, 14/08: capturar daqui pra frente). `enriquecer_motorista()` é chamada
pela Torre a cada carregamento do dia, aproveitando o que ela já resolve
(agent_id -> nome) sem nenhuma chamada nova à VUUPT.

`pedido_code` é sempre normalizado SEM o prefixo '#' (o código "cru" do
VUUPT/insucesso_entrega vem ora com, ora sem -- inconsistência conhecida,
ver buscar_servico_por_code em vuupt_client.py); `pedidos_historico.id_pedido`
guarda COM '#', então o JOIN normaliza dos dois lados.
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent
DB_PATH = _RAIZ / "dados" / "dados.db"

EVENTOS_POR_PEDIDO = (
    "INSUCESSO_DETECTADO", "REENVIO_AUTOMATICO", "REENVIO_AGENDADO",
    "AVISO_ENVIADO", "RESPOSTA_RECEBIDA",
    "EXCECAO_TRATADA", "EXCECAO_DESTRATADA",
)


def _normalizar_code(pedido_code: str) -> str:
    return (pedido_code or "").strip().lstrip("#")


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tratativas_pedido (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            pedido_code     TEXT NOT NULL,
            service_id      INTEGER,
            origem          TEXT NOT NULL,
            evento          TEXT NOT NULL,
            motivo_id       INTEGER,
            motivo_texto    TEXT,
            decisao         TEXT,
            motorista_nome  TEXT,
            rota_nome       TEXT,
            remetente_email TEXT,
            texto           TEXT,
            criado_em       TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tratativas_pedido_code ON tratativas_pedido(pedido_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tratativas_criado_em ON tratativas_pedido(criado_em)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tratativas_motivo_id ON tratativas_pedido(motivo_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tratativas_motorista ON tratativas_pedido(motorista_nome)")
    conn.commit()
    return conn


def registrar_evento(pedido_code: str, origem: str, evento: str, *,
                     service_id: int | None = None,
                     motivo_id: int | None = None,
                     motivo_texto: str | None = None,
                     decisao: str | None = None,
                     motorista_nome: str | None = None,
                     rota_nome: str | None = None,
                     remetente_email: str | None = None,
                     texto: str | None = None,
                     quando: str | None = None) -> None:
    """
    Registra um evento de tratativa. Nunca levanta exceção -- auditoria não
    pode derrubar o pipeline de insucesso/expedição que está chamando isso
    (mesmo princípio de historico.registrar_execucao).
    """
    try:
        pedido_code = _normalizar_code(pedido_code)
        if not pedido_code:
            logger.debug(f"registrar_evento({evento}): pedido_code vazio -- ignorado.")
            return
        agora = quando or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _conectar()
        conn.execute("""
            INSERT INTO tratativas_pedido
                (pedido_code, service_id, origem, evento, motivo_id, motivo_texto,
                 decisao, motorista_nome, rota_nome, remetente_email, texto, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pedido_code, service_id, origem, evento, motivo_id, motivo_texto,
              decisao, motorista_nome, rota_nome, remetente_email, texto, agora))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Falha ao registrar tratativa ({evento} / {pedido_code}): {e}")


def ja_registrado(pedido_code: str, evento: str) -> bool:
    """True se já existe uma linha com esse pedido_code+evento -- usado
    pelos pontos de escrita que rodam em janela deslizante (ex.:
    expedir_pedidos.py busca insucessos das últimas 48h a cada execução)
    pra não gerar um evento novo a cada ciclo pro mesmo insucesso."""
    pedido_code = _normalizar_code(pedido_code)
    if not pedido_code:
        return False
    conn = _conectar()
    try:
        row = conn.execute(
            "SELECT 1 FROM tratativas_pedido WHERE pedido_code = ? AND evento = ? LIMIT 1",
            (pedido_code, evento),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def enriquecer_motorista(pedido_code: str, motorista_nome: str | None, rota_nome: str | None) -> None:
    """
    Preenche motorista/rota nas tratativas do pedido que ainda não têm --
    não cria evento novo, só completa o que a Torre já resolveu ao vivo
    pra rota do dia (agent_id -> nome). Nunca levanta exceção.
    """
    if not motorista_nome:
        return
    try:
        pedido_code = _normalizar_code(pedido_code)
        if not pedido_code:
            return
        conn = _conectar()
        conn.execute("""
            UPDATE tratativas_pedido SET motorista_nome = ?, rota_nome = ?
            WHERE pedido_code = ? AND motorista_nome IS NULL
        """, (motorista_nome, rota_nome, pedido_code))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Falha ao enriquecer motorista da tratativa ({pedido_code}): {e}")


def buscar(filtros: dict | None = None, pagina: int = 1, por_pagina: int = 50) -> dict:
    """
    Busca paginada no histórico de tratativas, enriquecida com NF/embarcador/
    destinatário via LEFT JOIN em pedidos_historico (não duplicado aqui --
    dado sempre fresco). `filtros` aceita: busca (texto livre), motorista,
    motivo (motivo_texto), origem, evento, data_de, data_ate (sobre
    criado_em, formato YYYY-MM-DD).

    Retorna {"linhas": [...], "total": N, "pagina": P, "total_paginas": T}.
    """
    filtros = filtros or {}
    condicoes = []
    params = []

    busca = (filtros.get("busca") or "").strip()
    if busca:
        like = f"%{busca}%"
        condicoes.append("""(
            t.pedido_code LIKE ? OR t.motivo_texto LIKE ? OR t.texto LIKE ?
            OR t.motorista_nome LIKE ? OR p.numero_nfe LIKE ?
            OR p.cliente LIKE ? OR p.destinatario_nome LIKE ?
        )""")
        params.extend([like] * 7)

    if filtros.get("motorista"):
        condicoes.append("t.motorista_nome = ?")
        params.append(filtros["motorista"])
    if filtros.get("motivo"):
        condicoes.append("t.motivo_texto = ?")
        params.append(filtros["motivo"])
    if filtros.get("origem"):
        condicoes.append("t.origem = ?")
        params.append(filtros["origem"])
    if filtros.get("evento"):
        condicoes.append("t.evento = ?")
        params.append(filtros["evento"])
    if filtros.get("data_de"):
        condicoes.append("t.criado_em >= ?")
        params.append(f"{filtros['data_de']} 00:00:00")
    if filtros.get("data_ate"):
        condicoes.append("t.criado_em <= ?")
        params.append(f"{filtros['data_ate']} 23:59:59")

    where_sql = ("WHERE " + " AND ".join(condicoes)) if condicoes else ""
    join_sql = "LEFT JOIN pedidos_historico p ON REPLACE(p.id_pedido, '#', '') = t.pedido_code"

    conn = _conectar()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM tratativas_pedido t {join_sql} {where_sql}", params
        ).fetchone()[0]

        pagina = max(1, pagina)
        offset = (pagina - 1) * por_pagina
        rows = conn.execute(f"""
            SELECT t.*, p.numero_nfe, p.cliente AS embarcador, p.destinatario_nome, p.transportadora
            FROM tratativas_pedido t
            {join_sql}
            {where_sql}
            ORDER BY t.criado_em DESC
            LIMIT ? OFFSET ?
        """, params + [por_pagina, offset]).fetchall()
    finally:
        conn.close()

    total_paginas = max(1, (total + por_pagina - 1) // por_pagina)
    return {
        "linhas": [dict(r) for r in rows],
        "total": total,
        "pagina": pagina,
        "total_paginas": total_paginas,
    }


def valores_distintos(coluna: str) -> list[str]:
    """Valores distintos não vazios de uma coluna (pra popular selects de
    filtro: motorista_nome, motivo_texto...). `coluna` é validada contra uma
    lista fixa -- nunca vem de entrada do usuário na prática, mas não custa
    nada travar aqui também."""
    if coluna not in ("motorista_nome", "motivo_texto", "origem", "evento"):
        raise ValueError(f"coluna não permitida: {coluna}")
    conn = _conectar()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {coluna} FROM tratativas_pedido WHERE {coluna} IS NOT NULL AND {coluna} != '' ORDER BY {coluna}"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]
