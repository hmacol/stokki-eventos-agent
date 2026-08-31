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
import json
import logging
import re
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
STATUS_OFERTADA = "OFERTADA"
STATUS_ENVIADO = "ENVIADO"
STATUS_ERRO_ENVIO = "ERRO_ENVIO"
STATUS_DESCARTADO = "DESCARTADO"

# Acha o #N do nome da rota ('Planejamento - DD/MM/AAAA - #N', ou a
# mesma coisa com ' (cópia)' no final) -- usado tanto pra achar o
# próximo número livre (_proximo_numero_ordem) quanto pra ordenar as
# rotas na tela numericamente (listar_rascunhos_do_dia), não como
# texto (Hugo, 24/08: '#11' aparecia antes de '#2').
_PADRAO_NUMERO_ROTA = re.compile(r"#(\d+)")


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
            tipo_veiculo            TEXT,
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

    # Migração pra banco criado antes de 22/08 (horário padrão de
    # atendimento do destinatário, editável pelo menu de contexto da
    # tela de Planejamento -- ver regras/complexidade_entrega.py).
    if "horario_atendimento_inicio" not in colunas:
        conn.execute("ALTER TABLE rascunhos_parada ADD COLUMN horario_atendimento_inicio TEXT")
    if "horario_atendimento_fim" not in colunas:
        conn.execute("ALTER TABLE rascunhos_parada ADD COLUMN horario_atendimento_fim TEXT")

    # Migração pra banco criado antes de 15/08 (tipo de veículo grande,
    # ver regras/tipo_veiculo.py).
    colunas_rota = {row["name"] for row in conn.execute("PRAGMA table_info(rascunhos_rota)")}
    if "tipo_veiculo" not in colunas_rota:
        conn.execute("ALTER TABLE rascunhos_rota ADD COLUMN tipo_veiculo TEXT")

    # Migração 28/08: rota enviada pro motorista virtual LALAMOVE vira
    # também um pedido na Lalamove (ver lalamove_integracao.py).
    for coluna in ("lalamove_order_id", "lalamove_quotation_id", "lalamove_status",
                   "lalamove_share_link", "lalamove_preco", "lalamove_erro", "lalamove_atualizado_em",
                   "lalamove_veiculo", "lalamove_special_requests"):
        if coluna not in colunas_rota:
            conn.execute(f"ALTER TABLE rascunhos_rota ADD COLUMN {coluna} TEXT")

    conn.commit()
    return conn


def carregar_nf_por_codigo_pedido(codigos_base: set[str]) -> dict[str, str]:
    """
    {codigo_pedido BASE: "12345, 67890"} das Notas Fiscais já casadas e
    ENVIADAS na tabela documentos_processados (mesmo banco dados.db,
    populada por documentos_pedido/processar_documentos.py) -- pedido
    do Hugo, 14/08: achar/adicionar um pedido a uma rota pelo número da
    NF, na busca do pool/rotas.

    Reimplementado com sqlite3 puro (em vez de reaproveitar
    roteirizacao/gerar_pdf_romaneios.py::carregar_documentos_por_pedido)
    porque esse módulo importa PIL/pypdf/avisar_motoristas_rotas -- peso
    desnecessário num caminho chamado a cada carga da tela de
    planejamento, não só na hora de gerar 1 romaneio.

    codigos_base já deve vir normalizado (sem sufixo -R1/-R2 de
    reentrega, ver planejamento_rotas.py::_codigo_base) -- é sob o
    código BASE que o documento é casado (mesma convenção do
    romaneio). Pedido sem NF processada ainda (comum pra pedido muito
    novo, que acabou de entrar) simplesmente não entra no dict --
    ausência aqui não é erro.
    """
    if not codigos_base:
        return {}
    try:
        con = _conectar()
        try:
            nf_por_codigo: dict[str, set[str]] = {}
            lista = sorted(codigos_base)
            # SQLite limita em 999 variáveis por statement -- lotes de 900.
            for i in range(0, len(lista), 900):
                lote = lista[i:i + 900]
                marcadores = ",".join("?" * len(lote))
                for row in con.execute(
                        f"SELECT codigo_pedido, numero_nf FROM documentos_processados "
                        f"WHERE status='ENVIADO' AND tipo='Nota Fiscal' AND numero_nf IS NOT NULL "
                        f"AND codigo_pedido IN ({marcadores})", lote):
                    if row["numero_nf"]:
                        nf_por_codigo.setdefault(row["codigo_pedido"], set()).add(row["numero_nf"])
            return {codigo: ", ".join(sorted(numeros)) for codigo, numeros in nf_por_codigo.items()}
        finally:
            con.close()
    except Exception as e:
        logger.warning(f"Falha ao carregar NF por código de pedido: {e}")
        return {}


def _parada_de_servico(servico: dict, remetentes_por_id: dict[int, str]) -> dict:
    """Extrai de um dict de serviço (formato bruto da VUUPT, já com
    '_nivel_dificuldade' injetado por criar_rotas_diarias.py) os campos
    gravados em rascunhos_parada."""
    from roteirizacao_dados import extrair_volume_caixas, extrair_nivel_dificuldade, extrair_horario_atendimento

    sender_id = servico.get("sender_id")
    lat, lng = servico.get("latitude"), servico.get("longitude")
    horario_inicio, horario_fim = extrair_horario_atendimento(servico)
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
        "horario_atendimento_inicio": horario_inicio,
        "horario_atendimento_fim": horario_fim,
    }


def criar_lote_rascunhos(data_alvo: date, rascunhos: list[dict], lote_id: str | None = None) -> str:
    """
    Grava um lote novo de rascunhos numa única transação.

    `rascunhos` é uma lista de dicts com as chaves: nome, particao,
    tipo_rota, zona, tipo_veiculo, agent_id, vehicle_id, motorista_nome,
    start_location_base_id, end_location_base_id, start_at,
    km_estimado, sublote (lista de serviços brutos da VUUPT).

    `lote_id` explícito ACRESCENTA os rascunhos a um lote já existente
    em vez de abrir um novo -- botão "Roteirizar" da tela (Hugo, 12/08):
    a tela mostra sempre só o lote ativo mais recente, então um lote
    novo a cada roteirização da seleção esconderia os rascunhos que já
    estavam em edição.

    Retorna o lote_id usado (gerado ou repassado).
    """
    from mapa_util import carregar_remetentes_por_sender_id

    lote_id = lote_id or f"{data_alvo.isoformat()}-{uuid.uuid4().hex[:8]}"
    remetentes_por_id = carregar_remetentes_por_sender_id()

    conn = _conectar()
    try:
        for r in rascunhos:
            cursor = conn.execute("""
                INSERT INTO rascunhos_rota (
                    data_alvo, lote_id, nome, particao, tipo_rota, zona, tipo_veiculo,
                    agent_id, vehicle_id, motorista_nome,
                    start_location_base_id, end_location_base_id,
                    start_at, km_estimado, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data_alvo.isoformat(), lote_id, r["nome"], r.get("particao"),
                r.get("tipo_rota"), r.get("zona"), r.get("tipo_veiculo"), r.get("agent_id"), r.get("vehicle_id"),
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
                        nivel_dificuldade, volume_caixas,
                        horario_atendimento_inicio, horario_atendimento_fim
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rascunho_id, ordem, parada["service_id"], parada["codigo"], parada["titulo"],
                    parada["endereco"], parada["latitude"], parada["longitude"], parada["sender_id"],
                    parada["remetente_nome"], parada["destinatario_nome"],
                    parada["nivel_dificuldade"], parada["volume_caixas"],
                    parada["horario_atendimento_inicio"], parada["horario_atendimento_fim"],
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


def _chave_ordem_rota(nome: str | None) -> tuple:
    """Ordena pelo #N numérico do nome (não como texto -- senão '#11'
    cai antes de '#2'); rascunho sem #N reconhecível (renomeado à mão)
    vai pro fim, em ordem alfabética entre si."""
    match = _PADRAO_NUMERO_ROTA.search(nome or "")
    if match:
        return (0, int(match.group(1)), nome or "")
    return (1, 0, nome or "")


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
        """, (lote_id, STATUS_DESCARTADO)).fetchall()
        rows = sorted(rows, key=lambda r: _chave_ordem_rota(r["nome"]))
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
                nivel_dificuldade, volume_caixas,
                horario_atendimento_inicio, horario_atendimento_fim
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rascunho_destino_id, nova_ordem, parada["service_id"], parada["codigo"], parada["titulo"],
            parada["endereco"], parada["latitude"], parada["longitude"], parada["sender_id"],
            parada["remetente_nome"], parada["destinatario_nome"],
            parada["nivel_dificuldade"], parada["volume_caixas"],
            parada["horario_atendimento_inicio"], parada["horario_atendimento_fim"],
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


def atualizar_endereco_parada(rascunho_id: int, service_id: int, endereco: str,
                               latitude: float | None, longitude: float | None) -> bool:
    """Atualiza o endereço/coordenadas cacheados de uma parada já em
    rascunho -- opção "Editar endereço" do menu de contexto (Hugo,
    18/08). Necessário porque a rota em rascunho lê de rascunhos_parada
    (cópia local gravada quando o pedido entrou no rascunho), não ao
    vivo da VUUPT: sem isto, editar o endereço na VUUPT via
    planejamento_rotas.editar_endereco_pedido não refletiria numa
    parada que já está numa rota.

    Retorna True se achou e atualizou a parada, False se ela não está
    (mais) nesse rascunho -- chamador trata como não-crítico, igual
    _recalcular_km_silencioso."""
    conn = _conectar()
    try:
        cursor = conn.execute(
            "UPDATE rascunhos_parada SET endereco = ?, latitude = ?, longitude = ? "
            "WHERE rascunho_id = ? AND service_id = ?",
            (endereco, latitude, longitude, rascunho_id, service_id),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return False
        _tocar(conn, rascunho_id)
        _recalcular_km_silencioso(conn, rascunho_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def atualizar_nivel_horario_parada(rascunho_id: int, service_id: int, nivel: int,
                                    horario_inicio: str, horario_fim: str) -> bool:
    """Atualiza o nível de dificuldade/horário de atendimento cacheados de
    uma parada já em rascunho -- opção "Nível / horário de atendimento"
    do menu de contexto (Hugo, 22/08), mesmo padrão de
    atualizar_endereco_parada. O ajuste "de verdade" (que vale pra
    qualquer pedido futuro do mesmo cliente) é gravado à parte por
    regras.complexidade_entrega.definir_ajuste_manual; esta função só
    mantém a cópia local coerente pra quem já está numa rota não ficar
    mostrando o valor antigo até o próximo recarregamento.

    Retorna True se achou e atualizou a parada, False se ela não está
    (mais) nesse rascunho."""
    conn = _conectar()
    try:
        cursor = conn.execute(
            "UPDATE rascunhos_parada SET nivel_dificuldade = ?, "
            "horario_atendimento_inicio = ?, horario_atendimento_fim = ? "
            "WHERE rascunho_id = ? AND service_id = ?",
            (nivel, horario_inicio, horario_fim, rascunho_id, service_id),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return False
        _tocar(conn, rascunho_id)
        conn.commit()
        return True
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


def inverter_ordem(rascunho_id: int):
    """Inverte a ordem de execução das paradas -- botão "Inverter rota"
    da tela (Hugo, 17/08), útil quando o trajeto calculado fica melhor
    rodado de trás pra frente (ex: fim do dia perto de casa do
    motorista). Não precisa recalcular km (mesmo trajeto, sentido
    oposto -- soma das distâncias é a mesma), mas chama
    _recalcular_km_silencioso do mesmo jeito que reordenar_paradas/
    mover_parada, pra não deixar essa função como exceção do padrão."""
    conn = _conectar()
    try:
        paradas = conn.execute(
            "SELECT id FROM rascunhos_parada WHERE rascunho_id = ? ORDER BY ordem", (rascunho_id,),
        ).fetchall()
        n = len(paradas)
        for i, p in enumerate(paradas):
            conn.execute("UPDATE rascunhos_parada SET ordem = ? WHERE id = ?", (n - 1 - i, p["id"]))
        _tocar(conn, rascunho_id)
        _recalcular_km_silencioso(conn, rascunho_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mover_paradas(itens: list[dict], rascunho_destino_id: int) -> int:
    """Move várias paradas de uma vez pro mesmo rascunho DESTINO --
    seleção múltipla + "Mover selecionados" da tela (Hugo, 17/08), mesma
    mecânica de mover_parada mas numa transação só (evita N recálculos
    de km/geocodificação pra um lote inteiro).

    `itens` é uma lista de {"service_id", "rascunho_origem_id"} -- cada
    parada pode vir de uma rota de origem diferente, a seleção não
    precisa ser de uma rota só. Parada cujo service_id já está no
    destino é ignorada (mesmo edge case aceito em duplicar_rascunho: dá
    pra existir o mesmo pedido em 2 rascunhos; aqui só evita estourar a
    UNIQUE(rascunho_id, service_id) -- não é tratado como erro).

    Retorna quantas paradas foram efetivamente movidas."""
    conn = _conectar()
    try:
        ids_destino = {row["service_id"] for row in conn.execute(
            "SELECT service_id FROM rascunhos_parada WHERE rascunho_id = ?", (rascunho_destino_id,),
        ).fetchall()}
        prox = conn.execute(
            "SELECT COALESCE(MAX(ordem), -1) + 1 AS prox FROM rascunhos_parada WHERE rascunho_id = ?",
            (rascunho_destino_id,),
        ).fetchone()["prox"]

        origens_tocadas = set()
        movidas = 0
        for item in itens:
            service_id, origem_id = item["service_id"], item["rascunho_origem_id"]
            if service_id in ids_destino:
                continue
            parada = conn.execute(
                "SELECT * FROM rascunhos_parada WHERE rascunho_id = ? AND service_id = ?",
                (origem_id, service_id),
            ).fetchone()
            if not parada:
                continue
            conn.execute("DELETE FROM rascunhos_parada WHERE id = ?", (parada["id"],))
            conn.execute("""
                INSERT INTO rascunhos_parada (
                    rascunho_id, ordem, service_id, codigo, titulo, endereco,
                    latitude, longitude, sender_id, remetente_nome, destinatario_nome,
                    nivel_dificuldade, volume_caixas,
                    horario_atendimento_inicio, horario_atendimento_fim
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rascunho_destino_id, prox + movidas, parada["service_id"], parada["codigo"], parada["titulo"],
                parada["endereco"], parada["latitude"], parada["longitude"], parada["sender_id"],
                parada["remetente_nome"], parada["destinatario_nome"],
                parada["nivel_dificuldade"], parada["volume_caixas"],
                parada["horario_atendimento_inicio"], parada["horario_atendimento_fim"],
            ))
            ids_destino.add(service_id)
            origens_tocadas.add(origem_id)
            movidas += 1

        for origem_id in origens_tocadas:
            _tocar(conn, origem_id)
            _recalcular_km_silencioso(conn, origem_id)
        if movidas:
            _tocar(conn, rascunho_destino_id)
            _recalcular_km_silencioso(conn, rascunho_destino_id)
        conn.commit()
        return movidas
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fundir_rascunhos(rascunho_origem_id: int, rascunho_destino_id: int) -> dict:
    """Funde um rascunho no outro -- botão "Fundir com" do card (Hugo,
    17/08): todas as paradas do rascunho ORIGEM passam pro DESTINO
    (mantendo a ordem relativa, acrescentadas ao fim) e o rascunho
    ORIGEM vira DESCARTADO (mesmo mecanismo de descartar_rascunho, sem
    apagar rascunhos_parada -- não precisa limpeza extra). Só entre
    rascunhos ainda em RASCUNHO (não dá pra fundir rota já ENVIADA por
    aqui, mesma trava do drag-and-drop entre cards).

    Parada com o mesmo service_id nos dois rascunhos (edge case aceito
    em duplicar_rascunho) não é duplicada no destino -- fica só a cópia
    que já estava lá; a do rascunho origem é descartada junto com ele
    (linha órfã, inofensiva: o pool só reaparece se NENHUM rascunho
    ativo tiver aquele service_id, e o destino continua com ele).

    Retorna {"paradas_movidas", "paradas_duplicadas"}."""
    if rascunho_origem_id == rascunho_destino_id:
        raise ValueError("Não dá pra fundir uma rota com ela mesma.")
    conn = _conectar()
    try:
        origem = conn.execute("SELECT * FROM rascunhos_rota WHERE id = ?", (rascunho_origem_id,)).fetchone()
        destino = conn.execute("SELECT * FROM rascunhos_rota WHERE id = ?", (rascunho_destino_id,)).fetchone()
        if not origem or not destino:
            raise ValueError("Rota de origem ou destino não encontrada.")
        if origem["status"] != STATUS_RASCUNHO or destino["status"] != STATUS_RASCUNHO:
            raise ValueError("Só dá pra fundir rotas ainda em rascunho (não enviadas).")

        ids_destino = {row["service_id"] for row in conn.execute(
            "SELECT service_id FROM rascunhos_parada WHERE rascunho_id = ?", (rascunho_destino_id,),
        ).fetchall()}
        paradas = conn.execute(
            "SELECT * FROM rascunhos_parada WHERE rascunho_id = ? ORDER BY ordem", (rascunho_origem_id,),
        ).fetchall()
        paradas_a_mover = [p for p in paradas if p["service_id"] not in ids_destino]
        prox = conn.execute(
            "SELECT COALESCE(MAX(ordem), -1) + 1 AS prox FROM rascunhos_parada WHERE rascunho_id = ?",
            (rascunho_destino_id,),
        ).fetchone()["prox"]
        for i, p in enumerate(paradas_a_mover):
            conn.execute(
                "UPDATE rascunhos_parada SET rascunho_id = ?, ordem = ? WHERE id = ?",
                (rascunho_destino_id, prox + i, p["id"]),
            )

        conn.execute(
            "UPDATE rascunhos_rota SET status = ?, atualizado_em = datetime('now','localtime') WHERE id = ?",
            (STATUS_DESCARTADO, rascunho_origem_id),
        )
        _tocar(conn, rascunho_destino_id)
        _recalcular_km_silencioso(conn, rascunho_destino_id)
        conn.commit()
        logger.info(f"Rascunho {rascunho_origem_id} fundido em {rascunho_destino_id}: "
                    f"{len(paradas_a_mover)} parada(s) movida(s).")
        return {"paradas_movidas": len(paradas_a_mover), "paradas_duplicadas": len(paradas) - len(paradas_a_mover)}
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
    remetente_nome, nivel_dificuldade, volume_caixas,
    horario_atendimento_inicio, horario_atendimento_fim) -- o endpoint da
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
                nivel_dificuldade, volume_caixas,
                horario_atendimento_inicio, horario_atendimento_fim
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rascunho_id, ordem, parada["service_id"], parada["codigo"], parada["titulo"],
            parada["endereco"], parada["latitude"], parada["longitude"], parada["sender_id"],
            parada["remetente_nome"], parada.get("destinatario_nome", ""),
            parada["nivel_dificuldade"], parada["volume_caixas"],
            parada.get("horario_atendimento_inicio", "00:00"), parada.get("horario_atendimento_fim", "23:59"),
        ))
        _tocar(conn, rascunho_id)
        _recalcular_km_silencioso(conn, rascunho_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def renomear_rascunho(rascunho_id: int, novo_nome: str):
    """Renomeia um rascunho -- botão de editar nome na tela de
    planejamento (Hugo, 16/08). Só edita o registro local; o front
    trava o botão pra rota já ENVIADA (o nome de verdade passa a ser o
    que está gravado na rota da VUUPT, renomear só aqui divergiria sem
    refletir lá)."""
    novo_nome = (novo_nome or "").strip()
    if not novo_nome:
        raise ValueError("Nome da rota não pode ficar vazio.")
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE rascunhos_rota SET nome = ?, atualizado_em = datetime('now','localtime') WHERE id = ?",
            (novo_nome, rascunho_id),
        )
        conn.commit()
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


def publicar_oferta(rascunho_id: int):
    """RASCUNHO -> OFERTADA -- botão 'Publicar para motoristas' (Hugo,
    22/08). Só a transição de status; o resumo/elegibilidade e o
    registro da oferta em si vivem em regras/ofertas_rota.py (chamado
    ANTES desta função por painel_agentes/planejamento_rotas.py::
    publicar_oferta_rascunho, que decide o resumo e quem é elegível)."""
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE rascunhos_rota SET status = ?, atualizado_em = datetime('now','localtime') "
            "WHERE id = ? AND status = ?",
            (STATUS_OFERTADA, rascunho_id, STATUS_RASCUNHO),
        )
        conn.commit()
    finally:
        conn.close()


def despublicar_oferta(rascunho_id: int):
    """OFERTADA -> RASCUNHO -- botão 'Despublicar' (Hugo, 22/08),
    desistência de publicar antes de qualquer motorista escolher (ver
    regras/ofertas_rota.cancelar_oferta pra a oferta em si, que recusa
    cancelar se já tiver sido ESCOLHIDA -- nesse caso o rascunho segue
    OFERTADA até o próximo pull aplicar a escolha)."""
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE rascunhos_rota SET status = ?, atualizado_em = datetime('now','localtime') "
            "WHERE id = ? AND status = ?",
            (STATUS_RASCUNHO, rascunho_id, STATUS_OFERTADA),
        )
        conn.commit()
    finally:
        conn.close()


def aplicar_escolha_motorista(rascunho_id: int, agent_id: int, vehicle_id: int | None, motorista_nome: str | None):
    """Aplica a escolha de um motorista sobre uma oferta (vinda do pull
    da VPS, ver roteirizacao/sincronizar_respostas_confirmacao.py) --
    grava o motorista escolhido e volta o rascunho pra RASCUNHO, pronto
    pro fluxo normal de revisão/'Confirmar e Enviar' que já existe
    (decisão do Hugo, 22/08: a escolha do motorista nunca dispara envio
    à Vuupt sozinha). Idempotente: reaplicar a mesma escolha (ex.: pull
    rodado de novo antes do rascunho sair de OFERTADA por outro motivo)
    só regrava os mesmos campos, sem efeito colateral."""
    conn = _conectar()
    try:
        conn.execute(
            "UPDATE rascunhos_rota SET agent_id = ?, vehicle_id = ?, motorista_nome = ?, "
            "status = ?, atualizado_em = datetime('now','localtime') WHERE id = ?",
            (agent_id, vehicle_id, motorista_nome, STATUS_RASCUNHO, rascunho_id),
        )
        conn.commit()
    finally:
        conn.close()


# Espelho de criar_rotas_diarias.BASE_LOCATION_ID (operational_base_id
# da base na VUUPT, confirmado em produção) -- importar criar_rotas_
# diarias aqui só pra ler a constante puxaria selecao_modelo/rotas_client
# e todo o resto do pipeline pra dentro do painel.
BASE_LOCATION_ID = 6950


def referencia_para_rascunho_manual(data_alvo: date) -> dict:
    """
    Campos que um rascunho criado NA TELA (nova rota vazia ou rota a
    partir da seleção) herda: do primeiro rascunho do lote ativo da
    data, quando existe; senão -- data ainda sem lote, achado do Hugo
    12/08: a tela travava com "Nenhum lote ativo para essa data" mesmo
    com pool na tela -- os MESMOS padrões que criar_rotas_diarias.py
    usaria (lote_id novo no padrão de criar_lote_rascunhos, base 6950
    na ida e na volta, saída 13:00Z, partição Seco/GRANDE_SP), e o
    rascunho criado passa a ser ele mesmo o lote ativo da data.
    """
    rascunhos_do_dia = listar_rascunhos_do_dia(data_alvo)
    if rascunhos_do_dia:
        r = rascunhos_do_dia[0]
        return {chave: r[chave] for chave in (
            "lote_id", "particao", "tipo_rota",
            "start_location_base_id", "end_location_base_id", "start_at",
        )}
    return {
        "lote_id": f"{data_alvo.isoformat()}-{uuid.uuid4().hex[:8]}",
        "particao": "Seco",
        "tipo_rota": "GRANDE_SP",
        "start_location_base_id": BASE_LOCATION_ID,
        "end_location_base_id": BASE_LOCATION_ID,
        "start_at": f"{data_alvo.isoformat()}T13:00:00Z",
    }


def _proximo_numero_ordem(conn: sqlite3.Connection, data_alvo: date) -> int:
    """Próximo #N livre pro nome da rota nessa data, seguindo o mesmo
    padrão nativo do VUUPT ('Planejamento - DD/MM/AAAA - #N') que
    criar_rotas_diarias.py já usa -- olha o nome de TODOS os rascunhos
    não descartados da data (manual ou gerado pelo pipeline) e continua
    a numeração dali, em vez de reiniciar em #1 e colidir com uma rota
    que já existe."""
    maior = 0
    for row in conn.execute(
        "SELECT nome FROM rascunhos_rota WHERE data_alvo = ? AND status != ?",
        (data_alvo.isoformat(), STATUS_DESCARTADO),
    ).fetchall():
        match = _PADRAO_NUMERO_ROTA.search(row["nome"] or "")
        if match:
            maior = max(maior, int(match.group(1)))
    return maior + 1


def criar_rascunho_vazio(data_alvo: date, lote_id: str, particao: str, tipo_rota: str,
                         start_location_base_id: int, end_location_base_id: int | None,
                         start_at: str) -> int:
    """Rascunho sem paradas, usado quando o Hugo cria uma rota extra na
    tela pra receber paradas arrastadas de uma rota cheia."""
    conn = _conectar()
    try:
        nome = f"Planejamento - {data_alvo.strftime('%d/%m/%Y')} - #{_proximo_numero_ordem(conn, data_alvo)}"
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


def criar_rascunho_com_paradas(data_alvo: date, lote_id: str, particao: str, tipo_rota: str,
                               start_location_base_id: int, end_location_base_id: int | None,
                               start_at: str, paradas: list[dict]) -> int:
    """criar_rascunho_vazio + paradas já dentro, numa transação só --
    botão 'Criar rota' da seleção múltipla do pool (Hugo, 12/08):
    selecionar vários cards e virar rascunho direto, sem criar rota
    vazia e arrastar um por um. `paradas` no mesmo formato processado
    de adicionar_parada (itens do pool, tal como carregados no GET)."""
    conn = _conectar()
    try:
        nome = f"Planejamento - {data_alvo.strftime('%d/%m/%Y')} - #{_proximo_numero_ordem(conn, data_alvo)}"
        cursor = conn.execute("""
            INSERT INTO rascunhos_rota (
                data_alvo, lote_id, nome, particao, tipo_rota,
                start_location_base_id, end_location_base_id, start_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data_alvo.isoformat(), lote_id, nome, particao, tipo_rota,
            start_location_base_id, end_location_base_id, start_at, STATUS_RASCUNHO,
        ))
        rascunho_id = cursor.lastrowid
        for ordem, parada in enumerate(paradas):
            conn.execute("""
                INSERT INTO rascunhos_parada (
                    rascunho_id, ordem, service_id, codigo, titulo, endereco,
                    latitude, longitude, sender_id, remetente_nome, destinatario_nome,
                    nivel_dificuldade, volume_caixas,
                    horario_atendimento_inicio, horario_atendimento_fim
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rascunho_id, ordem, parada["service_id"], parada["codigo"], parada["titulo"],
                parada["endereco"], parada["latitude"], parada["longitude"], parada["sender_id"],
                parada["remetente_nome"], parada.get("destinatario_nome", ""),
                parada["nivel_dificuldade"], parada["volume_caixas"],
                parada.get("horario_atendimento_inicio", "00:00"), parada.get("horario_atendimento_fim", "23:59"),
            ))
        _recalcular_km_silencioso(conn, rascunho_id)
        conn.commit()
        return rascunho_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_PADRAO_COPIA = re.compile(r"\s*\(c[oó]pia(?:\s+\d+)?\)\s*$", re.IGNORECASE)


def _nome_copia(conn: sqlite3.Connection, nome_original: str, lote_id: str) -> str:
    """'Rota X' -> 'Rota X (cópia)', incrementando se já existir uma
    cópia com esse nome no lote (e sem empilhar sufixo em cópia de
    cópia -- primeiro tira um '(cópia)'/'(cópia N)' que já esteja no
    fim do nome original)."""
    base = _PADRAO_COPIA.sub("", nome_original).rstrip() or nome_original
    nomes_existentes = {
        row["nome"] for row in conn.execute(
            "SELECT nome FROM rascunhos_rota WHERE lote_id = ? AND status != ?",
            (lote_id, STATUS_DESCARTADO),
        ).fetchall()
    }
    candidato = f"{base} (cópia)"
    n = 2
    while candidato in nomes_existentes:
        candidato = f"{base} (cópia {n})"
        n += 1
    return candidato


def duplicar_rascunho(rascunho_id: int) -> int:
    """
    Duplica um rascunho -- inclusive um já ENVIADO (pedido do Hugo,
    13/08: "duplicar tanto as que não foram quanto as que foram
    enviadas"). rascunhos_parada continua guardando as paradas de uma
    rota ENVIADA mesmo depois do envio (enviar_rascunho só as apaga se
    removidas por conflito), então a cópia é sempre um snapshot fiel do
    que está no card no momento do clique.

    A cópia nasce SEMPRE em status RASCUNHO, no MESMO lote da origem --
    e por nascer assim, cai automaticamente na mesma trava de edição do
    resto da tela (só RASCUNHO é editável), mesmo quando a origem já
    virou ENVIADO e está travada. Motorista/base/horário são herdados
    tal como estão na origem (ponto de partida pra ajuste manual, não
    uma rota vazia); vuupt_route_id/erro_envio/enviado_em NÃO são
    herdados (INSERT novo, ficam NULL).

    Pedido com o mesmo service_id em dois rascunhos do lote (origem e
    cópia) não é impedido aqui: se ambos forem enviados sem que a
    sobreposição seja editada antes, o segundo envio esbarra na MESMA
    checagem de conflito ao vivo contra a VUUPT que enviar_rascunho já
    faz pra qualquer dado desatualizado (criar_rota_removendo_conflitos)
    -- não é um risco novo introduzido aqui.

    Retorna o id do novo rascunho.
    """
    origem = buscar_rascunho(rascunho_id)
    if not origem:
        raise ValueError(f"Rascunho {rascunho_id} não encontrado.")

    conn = _conectar()
    try:
        nome_copia = _nome_copia(conn, origem["nome"], origem["lote_id"])
        cursor = conn.execute("""
            INSERT INTO rascunhos_rota (
                data_alvo, lote_id, nome, particao, tipo_rota, zona, tipo_veiculo,
                agent_id, vehicle_id, motorista_nome,
                start_location_base_id, end_location_base_id,
                start_at, km_estimado, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            origem["data_alvo"], origem["lote_id"], nome_copia, origem["particao"],
            origem["tipo_rota"], origem["zona"], origem.get("tipo_veiculo"), origem["agent_id"], origem["vehicle_id"],
            origem["motorista_nome"], origem["start_location_base_id"], origem["end_location_base_id"],
            origem["start_at"], origem["km_estimado"], STATUS_RASCUNHO,
        ))
        novo_id = cursor.lastrowid
        for p in origem["paradas"]:
            conn.execute("""
                INSERT INTO rascunhos_parada (
                    rascunho_id, ordem, service_id, codigo, titulo, endereco,
                    latitude, longitude, sender_id, remetente_nome, destinatario_nome,
                    nivel_dificuldade, volume_caixas,
                    horario_atendimento_inicio, horario_atendimento_fim
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                novo_id, p["ordem"], p["service_id"], p["codigo"], p["titulo"],
                p["endereco"], p["latitude"], p["longitude"], p["sender_id"],
                p["remetente_nome"], p["destinatario_nome"],
                p["nivel_dificuldade"], p["volume_caixas"],
                p.get("horario_atendimento_inicio", "00:00"), p.get("horario_atendimento_fim", "23:59"),
            ))
        conn.commit()
        logger.info(f"Rascunho {rascunho_id} duplicado -> {novo_id} ('{nome_copia}').")
        return novo_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def descartar_rascunho(rascunho_id: int, *, permitir_enviado: bool = False):
    """As paradas não são apagadas fisicamente aqui de propósito: o
    rascunho passa a status=DESCARTADO, e como o pool de não alocados é
    sempre 'not_assigned da VUUPT menos o que está em rascunho ATIVO',
    as paradas voltam pro pool sozinhas -- sem precisar de nenhuma
    limpeza extra em rascunhos_parada.

    Só descarta RASCUNHO/ERRO_ENVIO por padrão: com a tela aberta em
    paralelo em outra sessão, o id pode ter virado ENVIADO depois do
    render -- descartar aí deixaria a rota da VUUPT sem card local
    (fantasma). permitir_enviado=True é só pros fluxos internos que
    descartam um ENVIADO de propósito, com a rota da VUUPT já
    cancelada/inexistente (preparar_cancelamento_de_parada)."""
    permitidos = [STATUS_RASCUNHO, STATUS_ERRO_ENVIO]
    if permitir_enviado:
        permitidos.append(STATUS_ENVIADO)
    conn = _conectar()
    try:
        cursor = conn.execute(
            f"UPDATE rascunhos_rota SET status = ?, atualizado_em = datetime('now','localtime') "
            f"WHERE id = ? AND status IN ({','.join('?' * len(permitidos))})",
            (STATUS_DESCARTADO, rascunho_id, *permitidos),
        )
        if cursor.rowcount == 0:
            logger.warning(f"Descarte do rascunho {rascunho_id} ignorado -- status atual "
                           f"não é descartável (provável edição concorrente em outra sessão).")
        conn.commit()
    finally:
        conn.close()


def reverter_para_rascunho(rascunho_id: int):
    """Devolve um rascunho ENVIADO pro status RASCUNHO -- usado por
    cancelar_rota_enviada (Hugo, 18/08: cancelar não deve espalhar as
    paradas de volta pro pool, e sim deixar a MESMA rota editável de
    novo em "Pendentes de envio", pronta pra ajustar e reenviar).
    vuupt_route_id/enviado_em são limpos (a rota antiga não existe
    mais); motorista/veículo/paradas continuam como estavam.

    Os campos da corrida Lalamove (order_id/status/link/preço) também
    são limpos se a corrida já morreu (ou nunca existiu) -- sem isso,
    reenviar a rota mostrava a corrida velha no card e escondia o botão
    "Lançar na Lalamove". Corrida ainda ABERTA é preservada (cancelar
    na VUUPT não cancela na Lalamove -- pendência conhecida): apagar a
    referência aqui perderia o único rastro de uma corrida paga.
    lalamove_veiculo/special_requests ficam sempre (são escolha do
    card, não estado da corrida)."""
    from lalamove_client import STATUS_FINAIS

    conn = _conectar()
    try:
        row = conn.execute(
            "SELECT lalamove_order_id, lalamove_status, lalamove_share_link "
            "FROM rascunhos_rota WHERE id = ?", (rascunho_id,)).fetchone()
        corrida_aberta = bool(row and row["lalamove_order_id"]
                              and (row["lalamove_status"] or "") not in STATUS_FINAIS)
        if corrida_aberta:
            logger.warning(
                f"Rascunho {rascunho_id} revertido pra RASCUNHO com corrida Lalamove ainda aberta "
                f"(#{row['lalamove_order_id']}, status {row['lalamove_status']}) -- cancelar na VUUPT "
                f"não cancela na Lalamove; acompanhar em {row['lalamove_share_link'] or 'app da Lalamove'}.")
        limpeza_lalamove = "" if corrida_aberta else (
            ", lalamove_order_id = NULL, lalamove_quotation_id = NULL, lalamove_status = NULL,"
            " lalamove_share_link = NULL, lalamove_preco = NULL, lalamove_erro = NULL,"
            " lalamove_atualizado_em = NULL")
        conn.execute(f"""
            UPDATE rascunhos_rota
            SET status = ?, vuupt_route_id = NULL, enviado_em = NULL, erro_envio = NULL{limpeza_lalamove},
                atualizado_em = datetime('now','localtime')
            WHERE id = ?
        """, (STATUS_RASCUNHO, rascunho_id))
        conn.commit()
    finally:
        conn.close()


def reverter_por_vuupt_route_id(vuupt_route_id: int) -> int | None:
    """Mesma coisa que reverter_para_rascunho, mas achando o rascunho
    pelo vuupt_route_id -- pra rotinas externas que cancelam a rota
    direto na VUUPT sem passar pelo botão "Cancelar rota" do painel
    (ex: roteirizacao/cancelar_rotas_sem_motorista.py, rotina diária
    das 17h45, Hugo 19/08), mantendo o rascunho local sincronizado em
    vez de ficar como ENVIADO fantasma apontando pra uma rota que não
    existe mais. Retorna o rascunho_id revertido, ou None se a rota
    não tinha rascunho local (ex: criada pelo pipeline automático sem
    --gerar-rascunho, nunca passou por /planejamento)."""
    conn = _conectar()
    try:
        row = conn.execute(
            "SELECT id FROM rascunhos_rota WHERE vuupt_route_id = ? AND status = ?",
            (vuupt_route_id, STATUS_ENVIADO),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    reverter_para_rascunho(row["id"])
    return row["id"]


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

    # Espelho no núcleo próprio (Fase A do app de motoristas, ver
    # DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md) -- best-effort: qualquer
    # falha aqui NÃO pode desfazer o envio à VUUPT que acabou de acontecer.
    try:
        from nucleo.rotas import registrar_rota_enviada
        registrar_rota_enviada(rascunho_id, vuupt_route_id)
    except Exception as e:
        logger.warning(f"Rota VUUPT {vuupt_route_id} (rascunho {rascunho_id}) enviada, mas não espelhada no núcleo: {e}")


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
    # Motorista virtual LALAMOVE: a corrida NÃO é mais criada aqui
    # (Hugo, 30/08) -- o envio só cria a rota na VUUPT; a corrida (paga)
    # sai pelo botão "Lançar na Lalamove" do card (lancar_lalamove).
    return {"rascunho_id": rascunho_id, "ok": True, "vuupt_route_id": rota["id"], "codigos_removidos": codigos_removidos}


def lancar_lalamove(rascunho_id: int, token: str) -> dict:
    """Botão "Lançar na Lalamove" do card (Hugo, 30/08): separado do
    "Confirmar e enviar" -- a rota do motorista virtual LALAMOVE vira
    rota na VUUPT no envio e a corrida (paga) só é criada aqui, com o
    veículo/opcionais escolhidos no card. Idempotente via
    criar_pedido_para_rascunho (pedido já criado volta ja_existia).

    Retorna {"rascunho_id", "ok", "order_id"?, "preco"?, "erro"?}.
    """
    from lalamove_integracao import criar_pedido_para_rascunho, rascunho_e_lalamove

    rascunho = buscar_rascunho(rascunho_id)
    if not rascunho:
        return {"rascunho_id": rascunho_id, "ok": False, "erro": "Rascunho não encontrado."}
    if rascunho["status"] != STATUS_ENVIADO:
        return {"rascunho_id": rascunho_id, "ok": False,
                "erro": f"Rota ainda não enviada à VUUPT (status={rascunho['status']}) -- confirme o envio antes de lançar na Lalamove."}
    if not rascunho_e_lalamove(rascunho):
        return {"rascunho_id": rascunho_id, "ok": False, "erro": "Rota não está com o motorista virtual LALAMOVE."}

    try:
        resultado = criar_pedido_para_rascunho(rascunho_id, token)
    except Exception as e:
        logger.warning(f"Lançamento Lalamove do rascunho {rascunho_id} falhou: {e}")
        gravar_lalamove(rascunho_id, erro=str(e))
        return {"rascunho_id": rascunho_id, "ok": False, "erro": str(e)}
    return {"rascunho_id": rascunho_id, **resultado}


def definir_lalamove_veiculo(rascunho_id: int, codigo: str | None,
                             special_requests: list | None = None):
    """Seletor de veículo + opcionais Lalamove no card (Hugo, 29-30/08)
    -- só faz sentido antes de "Lançar na Lalamove"; depois o pedido já
    foi cotado.
    special_requests=None mantém os opcionais como estão; lista (mesmo
    vazia) substitui."""
    conn = _conectar()
    try:
        row = conn.execute("SELECT status FROM rascunhos_rota WHERE id = ?", (rascunho_id,)).fetchone()
        if not row:
            raise ValueError("Rascunho não encontrado.")
        if row["status"] == STATUS_ENVIADO:
            raise ValueError("Rota já enviada -- o veículo Lalamove não pode mais ser trocado.")
        conn.execute("UPDATE rascunhos_rota SET lalamove_veiculo = ? WHERE id = ?",
                     ((codigo or "").strip().upper() or None, rascunho_id))
        if special_requests is not None:
            lista = sorted({str(s).strip().upper() for s in special_requests if str(s).strip()})
            conn.execute("UPDATE rascunhos_rota SET lalamove_special_requests = ? WHERE id = ?",
                         (json.dumps(lista) if lista else None, rascunho_id))
        _tocar(conn, rascunho_id)
        conn.commit()
    finally:
        conn.close()


def gravar_lalamove(rascunho_id: int, **campos):
    """Atualiza as colunas lalamove_* do rascunho (só as passadas).
    Ex.: gravar_lalamove(id, order_id='...', status='ASSIGNING_DRIVER')."""
    permitidas = {"order_id", "quotation_id", "status", "share_link", "preco", "erro", "veiculo"}
    sets, valores = [], []
    for chave, valor in campos.items():
        if chave not in permitidas:
            raise ValueError(f"Campo lalamove desconhecido: {chave}")
        sets.append(f"lalamove_{chave} = ?")
        valores.append(None if valor is None else str(valor))
    if not sets:
        return
    sets.append("lalamove_atualizado_em = datetime('now','localtime')")
    valores.append(rascunho_id)
    conn = _conectar()
    try:
        conn.execute(f"UPDATE rascunhos_rota SET {', '.join(sets)} WHERE id = ?", valores)
        conn.commit()
    finally:
        conn.close()


def listar_rascunhos_lalamove_abertos() -> list[dict]:
    """Rascunhos ENVIADOS com pedido Lalamove ainda não em status final
    (pra sincronizar_lalamove.py acompanhar)."""
    conn = _conectar()
    try:
        rows = conn.execute("""
            SELECT * FROM rascunhos_rota
            WHERE status = ? AND lalamove_order_id IS NOT NULL AND lalamove_order_id != ''
              AND COALESCE(lalamove_status, '') NOT IN ('COMPLETED', 'CANCELED', 'REJECTED', 'EXPIRED')
            ORDER BY data_alvo, id
        """, (STATUS_ENVIADO,)).fetchall()
        return [_montar_rascunho(conn, r) for r in rows]
    finally:
        conn.close()


# Espelho de roteirizacao/incrementar_rotas.py::STATUS_ROTA_HOJE_LIBERADOS
# (status que indicam que o motorista ainda não começou a rodar) --
# importar aquele módulo aqui puxaria o pipeline inteiro (selecao_modelo,
# alocacao_motoristas...) só pra ler uma constante.
STATUS_ROTA_NAO_INICIADA = {"not_started", "assigned", "accepted", "not_assigned", "scheduled"}


def _rota_do_corpo(dados_rota) -> dict:
    """Extrai o dict da rota do corpo de GET /routes/{id} -- lida com
    qualquer um dos envelopes já vistos na API da VUUPT ({"route": {...}},
    {"data": {...}}, ou o objeto sem envelope nenhum) em vez de travar
    num formato só: buscar_rota() nunca tinha sido exercitada contra uma
    resposta real antes deste botão, então o formato exato não estava
    confirmado."""
    if not isinstance(dados_rota, dict):
        return {}
    for chave in ("route", "data"):
        valor = dados_rota.get(chave)
        if isinstance(valor, dict):
            return valor
    return dados_rota


def cancelar_rota_enviada(rascunho_id: int, token: str) -> dict:
    """
    Cancela de verdade na VUUPT uma rota já enviada (rascunho status ==
    ENVIADO) -- botão "Cancelar rota" da tela de planejamento (Hugo,
    14/08). Só é permitido se o status ATUAL da rota, buscado AO VIVO
    contra a API (não o que está gravado localmente, que nunca é
    atualizado depois do envio), ainda indica que o motorista não
    começou a rodar -- ver STATUS_ROTA_NAO_INICIADA. Rota já em
    deslocamento (ou em qualquer status não reconhecido) é recusada.

    services_action="unassign" (mesma convenção de reprocessar_rotas.py):
    os pedidos da rota voltam pra not_assigned na VUUPT. O rascunho
    local passa por reverter_para_rascunho -- volta a ser RASCUNHO
    editável, com as mesmas paradas/motorista, em vez de descartado
    (Hugo, 18/08: cancelar é "desfazer o envio", não "jogar fora a
    rota").

    Retorna {"rascunho_id", "ok", "erro"?}.
    """
    from rotas_client import buscar_rota, cancelar_rota

    rascunho = buscar_rascunho(rascunho_id)
    if not rascunho:
        return {"rascunho_id": rascunho_id, "ok": False, "erro": "Rascunho não encontrado."}
    if rascunho["status"] != STATUS_ENVIADO or not rascunho["vuupt_route_id"]:
        return {"rascunho_id": rascunho_id, "ok": False,
                "erro": f"Rascunho não está enviado à VUUPT (status={rascunho['status']})."}

    route_id = rascunho["vuupt_route_id"]
    try:
        dados_rota = buscar_rota(token, route_id)
    except Exception as e:
        resposta = getattr(e, "response", None)
        if resposta is not None and resposta.status_code == 404:
            # rota não existe mais na VUUPT (excluída de vez, não só
            # cancelada) -- mesmo tratamento de "já cancelada por outra
            # via" abaixo: nada pra cancelar lá, só sincroniza o local.
            reverter_para_rascunho(rascunho_id)
            return {"rascunho_id": rascunho_id, "ok": True}
        return {"rascunho_id": rascunho_id, "ok": False,
                "erro": f"Falha ao consultar a rota #{route_id} na VUUPT: {e}"}

    status_atual = _rota_do_corpo(dados_rota).get("status")
    if status_atual == "canceled":
        # já cancelada na VUUPT por outra via -- só sincroniza o local
        reverter_para_rascunho(rascunho_id)
        return {"rascunho_id": rascunho_id, "ok": True}
    if status_atual not in STATUS_ROTA_NAO_INICIADA:
        return {"rascunho_id": rascunho_id, "ok": False,
                "erro": f"Rota #{route_id} não pode ser cancelada por aqui (status atual na VUUPT: "
                        f"'{status_atual}') -- só rotas que ainda não iniciaram deslocamento."}

    try:
        cancelar_rota(token, route_id, services_action="unassign")
    except Exception as e:
        return {"rascunho_id": rascunho_id, "ok": False, "erro": str(e)}

    reverter_para_rascunho(rascunho_id)
    return {"rascunho_id": rascunho_id, "ok": True}


def preparar_cancelamento_de_parada(rascunho_id: int, service_id: int, token: str) -> dict:
    """
    Passo prévio ao cancelamento de verdade de um pedido (VuuptClient.
    cancelar_servico, chamado por quem invoca esta função -- ver
    planejamento_rotas.cancelar_pedido) quando ele está numa rota JÁ
    ENVIADA -- botão "Cancelar pedido" da tela de planejamento (Hugo,
    15/08), estendendo cancelar_rota_enviada pra cancelar 1 PARADA em
    vez da rota inteira.

    Por quê: a rota de verdade na VUUPT (api.vuupt.com/routes) ainda
    referencia o service_id mesmo depois de cancelado via DELETE
    /services/{id} (domínio separado, app.vuupt.com) -- sem tirar a
    referência antes, a rota fica com uma parada fantasma. Mesma trava
    de cancelar_rota_enviada: só mexe se a rota, checada AO VIVO contra
    a API (não o status gravado localmente, nunca atualizado depois do
    envio), ainda não iniciou deslocamento (STATUS_ROTA_NAO_INICIADA).

      - Pedido é a ÚLTIMA parada da rota: não dá pra ter rota com 0
        paradas -- cancela a ROTA INTEIRA (services_action="unassign",
        mesmo mecanismo de cancelar_rota_enviada) e descarta o
        rascunho local.
      - Rota com mais paradas: tira só essa parada via atualizar_rota
        (PUT /routes/{id} com o restante, na mesma ordem) -- a rota
        segue ENVIADA, só com 1 parada a menos (mesmo padrão que
        enviar_rascunho já usa pra pedido em conflito) -- e remove a
        parada do rascunho local (remover_parada).

    Se o rascunho não estiver ENVIADO (ainda em RASCUNHO, ERRO_ENVIO ou
    já DESCARTADO), não faz nada -- quem chama já sabe que cancelar o
    serviço sozinho (+ remover_parada, se for o caso) resolve.

    Retorna {"ok": True} ou {"ok": False, "erro": "..."}.
    """
    rascunho = buscar_rascunho(rascunho_id)
    if not rascunho:
        return {"ok": False, "erro": f"Rascunho {rascunho_id} não encontrado."}
    if rascunho["status"] != STATUS_ENVIADO:
        return {"ok": True}

    from rotas_client import atualizar_rota, buscar_rota, cancelar_rota

    route_id = rascunho["vuupt_route_id"]
    try:
        dados_rota = buscar_rota(token, route_id)
    except Exception as e:
        resposta = getattr(e, "response", None)
        if resposta is not None and resposta.status_code == 404:
            # rota não existe mais na VUUPT -- nada pra tirar de lá,
            # só sincroniza o local (mesmo tratamento de cancelar_rota_enviada)
            descartar_rascunho(rascunho_id, permitir_enviado=True)
            return {"ok": True}
        return {"ok": False, "erro": f"Falha ao consultar a rota #{route_id} na VUUPT: {e}"}

    status_atual = _rota_do_corpo(dados_rota).get("status")
    if status_atual == "canceled":
        descartar_rascunho(rascunho_id, permitir_enviado=True)
        return {"ok": True}
    if status_atual not in STATUS_ROTA_NAO_INICIADA:
        return {"ok": False,
                "erro": f"Rota #{route_id} não pode ser alterada por aqui (status atual na VUUPT: "
                        f"'{status_atual}') -- só rotas que ainda não iniciaram deslocamento."}

    ids_restantes = [p["service_id"] for p in rascunho["paradas"] if p["service_id"] != service_id]
    try:
        if not ids_restantes:
            cancelar_rota(token, route_id, services_action="unassign")
            descartar_rascunho(rascunho_id, permitir_enviado=True)
            return {"ok": True}
        atualizar_rota(token, route_id, ids_restantes)
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    remover_parada(rascunho_id, service_id)
    return {"ok": True}
