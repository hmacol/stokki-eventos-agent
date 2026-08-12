# -*- coding: utf-8 -*-
"""
rascunhos_rota.py

Camada de dados dos RASCUNHOS de rota -- estado intermediário local
(SQLite, dados/dados.db) entre "sublotes calculados pelo pipeline de
roteirização" e "rota criada de verdade na VUUPT" (plano do Hugo,
12/08: transformar o mapa de rotas, hoje só leitura, num ambiente de
planejamento com ajuste manual antes de enviar).

criar_rotas_diarias.py passa a gravar um LOTE de rascunhos aqui em vez
de chamar rotas_client.criar_rota() direto (modo --gerar-rascunho). A
tela de planejamento (painel_agentes/planejamento_rotas.py, Fase 2)
lê/edita esses rascunhos; só quando o Hugo confirma o envio (Fase 3) é
que rotas_client.criar_rota() é chamado de fato.

service_id é a chave de verdade de cada parada (mesma que
rotas_client.criar_rota()/atualizar_rota() usam) -- mover uma parada
entre rascunhos é só um DELETE+INSERT, sem precisar casar com nenhum
outro identificador (CPF/CNPJ/id interno).

Lote (`lote_id`): cada rodada do pipeline grava um lote novo, sem
apagar lotes anteriores (histórico/auditoria). A tela sempre trabalha
com o lote mais recente, ainda não DESCARTADO, de uma data_alvo --
`_lote_ativo_id` centraliza esse critério.
"""
import logging
import sqlite3
import sys
import uuid
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"

STATUS_RASCUNHO = "RASCUNHO"
STATUS_ENVIADO = "ENVIADO"
STATUS_ERRO_ENVIO = "ERRO_ENVIO"
STATUS_DESCARTADO = "DESCARTADO"


def _conectar() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rascunhos_rota (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            data_alvo               TEXT NOT NULL,
            lote_id                 TEXT NOT NULL,
            nome                    TEXT NOT NULL,
            particao                TEXT,
            tipo_rota               TEXT,
            zona                    TEXT,
            agent_id                INTEGER,
            vehicle_id              INTEGER,
            motorista_nome          TEXT,
            start_location_base_id  INTEGER NOT NULL,
            end_location_base_id    INTEGER,
            start_at                TEXT NOT NULL,
            km_estimado             REAL,
            status                  TEXT NOT NULL DEFAULT 'RASCUNHO',
            vuupt_route_id          INTEGER,
            erro_envio              TEXT,
            criado_em               TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            atualizado_em           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            enviado_em              TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rascunhos_parada (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            rascunho_id        INTEGER NOT NULL REFERENCES rascunhos_rota(id) ON DELETE CASCADE,
            ordem              INTEGER NOT NULL,
            service_id         INTEGER NOT NULL,
            codigo             TEXT,
            titulo             TEXT,
            endereco           TEXT,
            latitude           REAL,
            longitude          REAL,
            sender_id          INTEGER,
            remetente_nome     TEXT,
            destinatario_nome  TEXT,
            nivel_dificuldade  INTEGER,
            volume_caixas      INTEGER,
            criado_em          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rascunhos_rota_lote ON rascunhos_rota(lote_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rascunhos_rota_data ON rascunhos_rota(data_alvo, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rascunhos_parada_rascunho ON rascunhos_parada(rascunho_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_rascunhos_parada_unico ON rascunhos_parada(rascunho_id, service_id)")

    # Migração pra banco criado antes de 12/08 (CREATE TABLE IF NOT
    # EXISTS não adiciona coluna em tabela já existente).
    colunas = {row["name"] for row in conn.execute("PRAGMA table_info(rascunhos_parada)")}
    if "destinatario_nome" not in colunas:
        conn.execute("ALTER TABLE rascunhos_parada ADD COLUMN destinatario_nome TEXT")

    conn.commit()
    return conn


def _parada_de_servico(servico: dict, remetentes_por_id: dict[int, str]) -> dict:
    """Extrai de um dict de serviço (formato bruto da VUUPT, já com
    '_nivel_dificuldade' injetado por criar_rotas_diarias.py) os campos
    gravados em rascunhos_parada."""
    from roteirizacao_dados import extrair_volume_caixas, extrair_nivel_dificuldade

    sender_id = servico.get("sender_id")
    lat, lng = servico.get("latitude"), servico.get("longitude")
    return {
        "service_id": servico["id"],
        "codigo": servico.get("code", ""),
        "titulo": servico.get("title", ""),
        "endereco": servico.get("address", ""),
        "latitude": float(lat) if lat not in (None, "") else None,
        "longitude": float(lng) if lng not in (None, "") else None,
        "sender_id": sender_id,
        "remetente_nome": remetentes_por_id.get(sender_id, "Remetente não identificado"),
        "destinatario_nome": (servico.get("customer") or {}).get("name") or "",
        "nivel_dificuldade": extrair_nivel_dificuldade(servico),
        "volume_caixas": extrair_volume_caixas(servico),
    }


def criar_lote_rascunhos(data_alvo: date, rascunhos: list[dict]) -> str:
    """
    Grava um lote novo de rascunhos numa única transação.

    `rascunhos` é uma lista de dicts com as chaves: nome, particao,
    tipo_rota, zona, agent_id, vehicle_id, motorista_nome,
    start_location_base_id, end_location_base_id, start_at,
    km_estimado, sublote (lista de serviços brutos da VUUPT).

    Retorna o lote_id gerado (usado depois pra listar só esse lote).
    """
    from mapa_util import carregar_remetentes_por_sender_id

    lote_id = f"{data_alvo.isoformat()}-{uuid.uuid4().hex[:8]}"
    remetentes_por_id = carregar_remetentes_por_sender_id()

    conn = _conectar()
    try:
        for r in rascunhos:
            cursor = conn.execute("""
                INSERT INTO rascunhos_rota (
                    data_alvo, lote_id, nome, particao, tipo_rota, zona,
                    agent_id, vehicle_id, motorista_nome,
                    start_location_base_id, end_location_base_id,
                    start_at, km_estimado, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data_alvo.isoformat(), lote_id, r["nome"], r.get("particao"),
                r.get("tipo_rota"), r.get("zona"), r.get("agent_id"), r.get("vehicle_id"),
                r.get("motorista_nome"), r["start_location_base_id"], r.get("end_location_base_id"),
                r["start_at"], r.get("km_estimado"), STATUS_RASCUNHO,
            ))
            rascunho_id = cursor.lastrowid
            for ordem, servico in enumerate(r["sublote"]):
                parada = _parada_de_servico(servico, remetentes_por_id)
                conn.execute("""
                    INSERT INTO rascunhos_parada (
                        rascunho_id, ordem, service_id, codigo, titulo, endereco,
                        latitude, longitude, sender_id, remetente_nome, destinatario_nome,
                        nivel_dificuldade, volume_caixas
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rascunho_id, ordem, parada["service_id"], parada["codigo"], parada["titulo"],
                    parada["endereco"], parada["latitude"], parada["longitude"], parada["sender_id"],
                    parada["remetente_nome"], parada["destinatario_nome"],
                    parada["nivel_dificuldade"], parada["volume_caixas"],
                ))
        conn.commit()
        logger.info(f"Lote de rascunhos '{lote_id}' gravado: {len(rascunhos)} rascunho(s).")
        return lote_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _lote_ativo_id(conn: sqlite3.Connection, data_alvo: date) -> str | None:
    """Lote mais recente daquela data com pelo menos 1 rascunho não
    DESCARTADO -- é sempre esse que a tela mostra."""
    row = conn.execute("""
        SELECT lote_id FROM rascunhos_rota
        WHERE data_alvo = ? AND status != ?
        ORDER BY criado_em DESC LIMIT 1
    """, (data_alvo.isoformat(), STATUS_DESCARTADO)).fetchone()
    return row["lote_id"] if row else None


def _montar_rascunho(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    paradas = conn.execute(
        "SELECT * FROM rascunhos_parada WHERE rascunho_id = ? ORDER BY ordem",
        (row["id"],),
    ).fetchall()
    return {**dict(row), "paradas": [dict(p) for p in paradas]}


def listar_rascunhos_do_dia(data_alvo: date) -> list[dict]:
    """Rascunhos (com paradas aninhadas) do lote ativo mais recente da
    data, prontos pra tela de planejamento."""
    conn = _conectar()
    try:
        lote_id = _lote_ativo_id(conn, data_alvo)
        if not lote_id:
            return []
        rows = conn.execute("""
            SELECT * FROM rascunhos_rota
            WHERE lote_id = ? AND status != ?
            ORDER BY nome
        """, (lote_id, STATUS_DESCARTADO)).fetchall()
        return [_montar_rascunho(conn, r) for r in rows]
    finally:
        conn.close()


def buscar_rascunho(rascunho_id: int) -> dict | None:
    conn = _conectar()
    try:
        row = conn.execute("SELECT * FROM rascunhos_rota WHERE id = ?", (rascunho_id,)).fetchone()
        return _montar_rascunho(conn, row) if row else None
    finally:
        conn.close()


def _tocar(conn: sqlite3.Connection, rascunho_id: int):
    conn.execute(
        "UPDATE rascunhos_rota SET atualizado_em = datetime('now','localtime') WHERE id = ?",
        (rascunho_id,),
    )


ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"  # mesma base de mapa_rotas.py/criar_rotas_diarias.py


def _gmaps_key() -> str:
    import yaml
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return config.get("google_maps", {}).get("api_key", "")


def _recalcular_km_silencioso(conn: sqlite3.Connection, rascunho_id: int):
    """recalcular_km, mas nunca deixa a falha de geocodificação/rede
    derrubar a ação de edição em si (mover/reordenar/adicionar/remover
    parada) -- o km só fica desatualizado até a próxima edição bem-
    sucedida, não é crítico o suficiente pra bloquear o resto."""
    try:
        recalcular_km(conn, rascunho_id, _gmaps_key())
    except Exception as e:
        logger.warning(f"Falha ao recalcular km do rascunho {rascunho_id} (não afeta a edição): {e}")


def recalcular_km(conn: sqlite3.Connection, rascunho_id: int, api_key: str) -> float | None:
    """Recalcula e grava o km_estimado do rascunho a partir das paradas
    atuais. NÃO reaproveita roteirizacao_dados.calcular_km_estimado
    aqui de propósito: aquela função busca coordenada via
    obter_coordenadas(), que geocodifica pelo campo 'address' (cache de
    geocodificacao.py) e ignora latitude/longitude já presentes no
    dict -- correto pro pipeline automático (só tem endereço bruto da
    VUUPT), mas em rascunhos_parada as coordenadas JÁ estão resolvidas
    e gravadas; usar aquela função aqui geocodificaria 'None' e
    zeraria o km. Soma a MESMA fórmula (haversine, base -> p1 -> ... ->
    pN -> base) direto sobre as coordenadas salvas."""
    from roteirizacao_dados import _distancia_km
    from geocodificacao import geocodificar

    paradas = conn.execute(
        "SELECT latitude, longitude FROM rascunhos_parada WHERE rascunho_id = ? ORDER BY ordem",
        (rascunho_id,),
    ).fetchall()
    base = geocodificar(ENDERECO_BASE, api_key or "")
    coords = [(p["latitude"], p["longitude"]) for p in paradas if p["latitude"] and p["longitude"]]
    if not base or not coords:
        return None

    km = _distancia_km(base[0], base[1], *coords[0])
    for i in range(len(coords) - 1):
        km += _distancia_km(*coords[i], *coords[i + 1])
    km += _distancia_km(*coords[-1], base[0], base[1])

    conn.execute("UPDATE rascunhos_rota SET km_estimado = ? WHERE id = ?", (km, rascunho_id))
    return km


def mover_parada(service_id: int, rascunho_origem_id: int, rascunho_destino_id: int, nova_ordem: int):
    conn = _conectar()
    try:
        parada = conn.execute(
            "SELECT * FROM rascunhos_parada WHERE rascunho_id = ? AND service_id = ?",
            (rascunho_origem_id, service_id),
        ).fetchone()
        if not parada:
            raise ValueError(f"Parada {service_id} não encontrada no rascunho {rascunho_origem_id}.")
        conn.execute("DELETE FROM rascunhos_parada WHERE id = ?", (parada["id"],))
        conn.execute("""
            UPDATE rascunhos_parada SET ordem = ordem + 1
            WHERE rascunho_id = ? AND ordem >= ?
        """, (rascunho_destino_id, nova_ordem))
        conn.execute("""
            INSERT INTO rascunhos_parada (
                rascunho_id, ordem, service_id, codigo, titulo, endereco,
                latitude, longitude, sender_id, remetente_nome, destinatario_nome,
                nivel_dificuldade, volume_caixas
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rascunho_destino_id, nova_ordem, parada["service_id"], parada["codigo"], parada["titulo"],
            parada["endereco"], parada["latitude"], parada["longitude"], parada["sender_id"],
            parada["remetente_nome"], parada["destinatario_nome"],
            parada["nivel_dificuldade"], parada["volume_caixas"],
        ))
        _tocar(conn, rascunho_origem_id)
        _tocar(conn, rascunho_destino_id)
        _recalcular_km_silencioso(conn, rascunho_origem_id)
        _recalcular_km_silencioso(conn, rascunho_destino_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def otimizar_sequencia(rascunho_id: int):
    """Botão explícito 'Otimizar sequência' -- roda o MESMO 2-opt do
    pipeline automático (otimizacao_rotas.ordenar_2opt) sobre as
    paradas atuais do rascunho e grava a nova ordem. Só dispara quando
    o Hugo pede (nunca automaticamente a cada edição -- reordenar a
    rota inteira depois de mover 1 parada manualmente seria
    contraintuitivo numa ferramenta de ajuste manual)."""
    from otimizacao_rotas import ordenar_2opt
    from geocodificacao import geocodificar

    api_key = _gmaps_key()
    conn = _conectar()
    try:
        paradas = conn.execute(
            "SELECT * FROM rascunhos_parada WHERE rascunho_id = ? ORDER BY ordem", (rascunho_id,),
        ).fetchall()
        if len(paradas) <= 2:
            return
        base = geocodificar(ENDERECO_BASE, api_key or "")
        if not base:
            raise ValueError("Não consegui geocodificar a base -- não dá pra otimizar sem ela.")

        servicos_fake = [{"id": p["service_id"], "latitude": p["latitude"], "longitude": p["longitude"]} for p in paradas]
        nova_ordem = ordenar_2opt(servicos_fake, base[0], base[1], api_key)
        for ordem, s in enumerate(nova_ordem):
            conn.execute(
                "UPDATE rascunhos_parada SET ordem = ? WHERE rascunho_id = ? AND service_id = ?",
                (ordem, rascunho_id, s["id"]),
            )
        _tocar(conn, rascunho_id)
        _recalcular_km_silencioso(conn, rascunho_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reordenar_paradas(rascunho_id: int, ordem_service_ids: list[int]):
    conn = _conectar()
    try:
        for ordem, service_id in enumerate(ordem_service_ids):
            conn.execute(
                "UPDATE rascunhos_parada SET ordem = ? WHERE rascunho_id = ? AND service_id = ?",
                (ordem, rascunho_id, service_id),
            )
        _tocar(conn, rascunho_id)
        _recalcular_km_silencioso(conn, rascunho_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remover_parada(rascunho_id: int, service_id: int) -> dict | None:
    """Retorna os dados completos da parada removida (mesmas chaves de
    _parada_de_servico) pra quem chama (endpoint da tela) devolver ela
    pro pool sem precisar de reload nem depender de um estado antigo
    guardado no front."""
    conn = _conectar()
    try:
        row = conn.execute(
            "SELECT * FROM rascunhos_parada WHERE rascunho_id = ? AND service_id = ?",
            (rascunho_id, service_id),
        ).fetchone()
        conn.execute(
            "DELETE FROM rascunhos_parada WHERE rascunho_id = ? AND service_id = ?",
            (rascunho_id, service_id),
        )
        _tocar(conn, rascunho_id)
        _recalcular_km_silencioso(conn, rascunho_id)
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def adicionar_parada(rascunho_id: int, parada: dict, ordem: int | None = None):
    """`parada` já vem no formato processado (mesmas chaves de
    _parada_de_servico/planejamento_rotas._servico_para_pool: service_id,
    codigo, titulo, endereco, latitude, longitude, sender_id,
    remetente_nome, nivel_dificuldade, volume_caixas) -- o endpoint da
    tela repassa o item do pool tal como já foi carregado no GET
    /planejamento, sem precisar bater na VUUPT de novo."""
    conn = _conectar()
    try:
        if ordem is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(ordem), -1) + 1 AS prox FROM rascunhos_parada WHERE rascunho_id = ?",
                (rascunho_id,),
            ).fetchone()
            ordem = row["prox"]
        conn.execute("""
            INSERT INTO rascunhos_parada (
                rascunho_id, ordem, service_id, codigo, titulo, endereco,
                latitude, longitude, sender_id, remetente_nome, destinatario_nome,
                nivel_dificuldade, volume_caixas
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rascunho_id, ordem, parada["service_id"], parada["codigo"], parada["titulo"],
            parada["endereco"], parada["latitude"], parada["longitude"], parada["sender_id"],
            parada["remetente_nome"], parada.get("destinatario_nome", ""),
            parada["nivel_dificuldade"], parada["volume_caixas"],
        ))
        _tocar(conn, rascunho_id)
        _recalcular_km_silencioso(conn, rascunho_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def trocar_motorista(rascunho_id: int, agent_id: int | None, vehicle_id: int | None, motorista_nome: str | None):
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE rascunhos_rota SET agent_id = ?, vehicle_id = ?, motorista_nome = ? WHERE id = ?",
            (agent_id, vehicle_id, motorista_nome, rascunho_id),
        )
        _tocar(conn, rascunho_id)
        conn.commit()
    finally:
        conn.close()


def criar_rascunho_vazio(data_alvo: date, lote_id: str, particao: str, tipo_rota: str,
                         start_location_base_id: int, end_location_base_id: int | None,
                         start_at: str) -> int:
    """Rascunho sem paradas, usado quando o Hugo cria uma rota extra na
    tela pra receber paradas arrastadas de uma rota cheia."""
    conn = _conectar()
    try:
        nome = f"Planejamento - {data_alvo.strftime('%d/%m/%Y')} - manual-{uuid.uuid4().hex[:6]}"
        cursor = conn.execute("""
            INSERT INTO rascunhos_rota (
                data_alvo, lote_id, nome, particao, tipo_rota,
                start_location_base_id, end_location_base_id, start_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data_alvo.isoformat(), lote_id, nome, particao, tipo_rota,
            start_location_base_id, end_location_base_id, start_at, STATUS_RASCUNHO,
        ))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def descartar_rascunho(rascunho_id: int):
    """As paradas não são apagadas fisicamente aqui de propósito: o
    rascunho passa a status=DESCARTADO, e como o pool de não alocados é
    sempre 'not_assigned da VUUPT menos o que está em rascunho ATIVO',
    as paradas voltam pro pool sozinhas -- sem precisar de nenhuma
    limpeza extra em rascunhos_parada."""
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE rascunhos_rota SET status = ?, atualizado_em = datetime('now','localtime') WHERE id = ?",
            (STATUS_DESCARTADO, rascunho_id),
        )
        conn.commit()
    finally:
        conn.close()


def marcar_enviado(rascunho_id: int, vuupt_route_id: int):
    conn = _conectar()
    try:
        conn.execute("""
            UPDATE rascunhos_rota
            SET status = ?, vuupt_route_id = ?, erro_envio = NULL, enviado_em = datetime('now','localtime')
            WHERE id = ?
        """, (STATUS_ENVIADO, vuupt_route_id, rascunho_id))
        conn.commit()
    finally:
        conn.close()


def marcar_erro_envio(rascunho_id: int, mensagem: str):
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE rascunhos_rota SET status = ?, erro_envio = ? WHERE id = ?",
            (STATUS_ERRO_ENVIO, mensagem, rascunho_id),
        )
        conn.commit()
    finally:
        conn.close()


def enviar_rascunho(rascunho_id: int, token: str) -> dict:
    """
    Confirma e envia UM rascunho pra VUUPT de verdade (Fase 3: botão
    'Confirmar e Enviar' da tela de planejamento) -- rotas_client.
    criar_rota_removendo_conflitos() com os service_ids na ordem atual
    do rascunho, agent_id/vehicle_id escolhidos, start_at/bases
    gravados na criação do rascunho.

    Idempotente: rascunho que não está mais em status RASCUNHO (já
    ENVIADO, DESCARTADO, ou já em ERRO_ENVIO de uma tentativa anterior
    -- esse último só é reenviado se chamado de novo explicitamente,
    nunca automaticamente) é pulado sem reprocessar.

    Se algum(ns) pedido(s) do rascunho já estavam em outra rota
    (conflito com dado desatualizado, mesmo achado de produção que
    motivou criar_rota_removendo_conflitos), a rota é criada só com o
    restante e os pedidos conflitantes voltam pro pool (removidos do
    rascunho, sem apagar o rascunho -- ele fica ENVIADO com menos
    paradas que originalmente).

    Retorna {"rascunho_id", "ok", "vuupt_route_id"?, "codigos_removidos"?, "erro"?}.
    """
    from rotas_client import criar_rota_removendo_conflitos
    from fingerprint_rotas import marcar_alocado

    rascunho = buscar_rascunho(rascunho_id)
    if not rascunho:
        return {"rascunho_id": rascunho_id, "ok": False, "erro": "Rascunho não encontrado."}
    if rascunho["status"] != STATUS_RASCUNHO:
        return {"rascunho_id": rascunho_id, "ok": False, "erro": f"Rascunho não está pendente de envio (status={rascunho['status']})."}
    if not rascunho["paradas"]:
        marcar_erro_envio(rascunho_id, "Rascunho sem paradas -- nada pra enviar.")
        return {"rascunho_id": rascunho_id, "ok": False, "erro": "Rascunho sem paradas."}

    sublote = [{"id": p["service_id"], "code": p["codigo"]} for p in rascunho["paradas"]]

    try:
        rota, sublote_criado, codigos_removidos = criar_rota_removendo_conflitos(
            token, rascunho["nome"], rascunho["start_at"], sublote,
            start_location_base_id=rascunho["start_location_base_id"],
            end_location_base_id=rascunho["end_location_base_id"],
            agent_id=rascunho["agent_id"], vehicle_id=rascunho["vehicle_id"],
        )
    except Exception as e:
        marcar_erro_envio(rascunho_id, str(e))
        return {"rascunho_id": rascunho_id, "ok": False, "erro": str(e)}

    if rota is None:
        mensagem = f"Todos os {len(sublote)} pedido(s) já estavam em outra rota -- nenhum enviado."
        marcar_erro_envio(rascunho_id, mensagem)
        return {"rascunho_id": rascunho_id, "ok": False, "erro": mensagem}

    for codigo in codigos_removidos:
        parada = next((p for p in rascunho["paradas"] if p["codigo"] == codigo), None)
        if parada:
            remover_parada(rascunho_id, parada["service_id"])

    for s in sublote_criado:
        marcar_alocado(s["id"], rota["id"])

    marcar_enviado(rascunho_id, rota["id"])
    return {"rascunho_id": rascunho_id, "ok": True, "vuupt_route_id": rota["id"], "codigos_removidos": codigos_removidos}
