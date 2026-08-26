# -*- coding: utf-8 -*-
"""
nucleo/operacao.py

Regras de operação do motorista sobre o núcleo (Fase B) -- o que o app
faz: aceitar/recusar rota, iniciar, registrar evento de parada (chegada,
entregue, parcial, insucesso) com checklist, comprovantes, GPS,
finalizar rota, ofertas (marketplace) e disponibilidade.

Camada pura (recebe a conexão, devolve dicts, levanta OperacaoInvalida)
-- a API HTTP (nucleo/api_motorista.py) é só casca; o painel pode chamar
as mesmas funções (ex.: operador registrando insucesso por telefone).

Regras fixas:
- O motorista só enxerga/mexe em rota cujo agent_id é o dele.
- Escrita de operação (iniciar, eventos, finalizar) só em rota com
  provedor=APP. Rota VUUPT é somente leitura aqui (o motorista opera no
  app da VUUPT enquanto ela existir) -- exceto aceitar/recusar, que
  também alimenta `confirmacoes_rota` pro badge do Planejamento.
- Todo evento que vem do app carrega `uuid` gerado no aparelho: a fila
  offline reenvia à vontade, o banco ignora repetido.
"""
import json
import math
import sqlite3
from datetime import date, timedelta

from nucleo import banco, pedidos as nucleo_pedidos
from nucleo.rotas import registrar_evento

TIPOS_EVENTO_PARADA = {"CHEGADA", "ENTREGUE", "PARCIAL", "INSUCESSO", "OBSERVACAO"}
_SITUACAO_DO_EVENTO = {
    "ENTREGUE": banco.PARADA_ENTREGUE,
    "PARCIAL": banco.PARADA_PARCIAL,
    "INSUCESSO": banco.PARADA_INSUCESSO,
}
_STATUS_PEDIDO_DA_SITUACAO = {
    banco.PARADA_ENTREGUE: banco.PEDIDO_ENTREGUE,
    banco.PARADA_PARCIAL: banco.PEDIDO_ENTREGUE,
    banco.PARADA_INSUCESSO: banco.PEDIDO_INSUCESSO,
}
TIPOS_COMPROVANTE = {"CANHOTO", "ASSINATURA", "NF_DEVOLUCAO", "PRODUTO", "OCORRENCIA", "DOCUMENTO"}

# Filtro de sanidade do km por GPS: ponto impreciso ou salto absurdo
# entre leituras não entra na soma.
GPS_PRECISAO_MAX_M = 100.0
GPS_SALTO_MAX_KM = 20.0


class OperacaoInvalida(Exception):
    def __init__(self, mensagem: str, codigo: int = 400):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.codigo = codigo


def _tem_tabela(conn: sqlite3.Connection, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone() is not None


def _json(v) -> str | None:
    return json.dumps(v, ensure_ascii=False, default=str) if v is not None else None


# ── Rotas ──────────────────────────────────────────────────────────────────────

def rota_do_motorista(conn: sqlite3.Connection, rota_id: int, agent_id: int | None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone()
    if not row or agent_id is None or row["agent_id"] != agent_id:
        raise OperacaoInvalida("Rota não encontrada.", 404)
    return row


def _exigir_provedor_app(rota: sqlite3.Row):
    if rota["provedor"] != banco.PROVEDOR_APP:
        raise OperacaoInvalida("Essa rota é operada pelo app da VUUPT -- registre a entrega por lá.", 409)


def listar_rotas_motorista(conn: sqlite3.Connection, agent_id: int | None, de: date, ate: date) -> list[dict]:
    if agent_id is None:
        return []
    rows = conn.execute("""
        SELECT * FROM nucleo_rotas
        WHERE agent_id = ? AND data_rota BETWEEN ? AND ? AND status != ?
        ORDER BY data_rota, start_at, nome
    """, (agent_id, de.isoformat(), ate.isoformat(), banco.ROTA_CANCELADA)).fetchall()
    return [montar_rota(conn, r) for r in rows]


def montar_rota(conn: sqlite3.Connection, rota: sqlite3.Row) -> dict:
    d = dict(rota)
    d.pop("dados_json", None)
    d["editavel"] = rota["provedor"] == banco.PROVEDOR_APP
    paradas = conn.execute("SELECT * FROM nucleo_paradas WHERE rota_id = ? ORDER BY ordem", (rota["id"],)).fetchall()
    d["paradas"] = [montar_parada(conn, p) for p in paradas]
    # Confirmação (badge do Planejamento) quando a rota é da VUUPT
    d["confirmacao"] = None
    if rota["vuupt_route_id"] and _tem_tabela(conn, "confirmacoes_rota"):
        c = conn.execute("SELECT status, respondido_em, motivo_recusa FROM confirmacoes_rota WHERE vuupt_route_id = ? AND agent_id = ?",
                         (rota["vuupt_route_id"], rota["agent_id"])).fetchone()
        d["confirmacao"] = dict(c) if c else None
    return d


def montar_parada(conn: sqlite3.Connection, p: sqlite3.Row) -> dict:
    d = dict(p)
    d.pop("dados_json", None)
    # Pedido: telefone/horário de atendimento vêm do espelho de pedidos quando existir
    if p["codigo"]:
        ped = conn.execute("SELECT destinatario_telefone, horario_inicio, horario_fim, destinatario_nome FROM nucleo_pedidos WHERE codigo = ?",
                           (p["codigo"],)).fetchone()
        if ped:
            d["telefone"] = ped["destinatario_telefone"]
            d["janela_inicio"] = d["janela_inicio"] or ped["horario_inicio"]
            d["janela_fim"] = d["janela_fim"] or ped["horario_fim"]
            d["destinatario_nome"] = d["destinatario_nome"] or ped["destinatario_nome"]
    d["comprovantes"] = [
        {"id": c["id"], "tipo": c["tipo"], "uuid": c["uuid"], "capturado_em": c["capturado_em"]}
        for c in conn.execute("SELECT id, tipo, uuid, capturado_em FROM nucleo_comprovantes WHERE parada_id = ? ORDER BY id", (p["id"],))
    ]
    return d


def _atualizar_confirmacao_vuupt(conn: sqlite3.Connection, rota: sqlite3.Row, status: str, motivo: str | None):
    """Rota VUUPT: espelha a resposta em confirmacoes_rota (badge do
    /planejamento) -- só se já existe a linha (criada pelo aviso de rota)."""
    if not rota["vuupt_route_id"] or not _tem_tabela(conn, "confirmacoes_rota"):
        return
    conn.execute("""
        UPDATE confirmacoes_rota SET status = ?, motivo_recusa = ?, respondido_em = ?
        WHERE vuupt_route_id = ? AND agent_id = ?
    """, (status, motivo, banco.agora(), rota["vuupt_route_id"], rota["agent_id"]))


def aceitar_rota(conn: sqlite3.Connection, rota_id: int, agent_id: int, ocorrido_em: str | None = None,
                 latitude=None, longitude=None, uuid: str | None = None) -> dict:
    rota = rota_do_motorista(conn, rota_id, agent_id)
    if rota["status"] in (banco.ROTA_CANCELADA, banco.ROTA_CONCLUIDA):
        raise OperacaoInvalida(f"Rota já está {rota['status'].lower()}.", 409)
    if rota["status"] == banco.ROTA_PLANEJADA:
        conn.execute("UPDATE nucleo_rotas SET status = ?, atualizado_em = ? WHERE id = ?",
                     (banco.ROTA_ACEITA, banco.agora(), rota_id))
    registrar_evento(conn, "ROTA_ACEITA", banco.ORIGEM_APP, ocorrido_em, rota_id=rota_id, agent_id=agent_id,
                     latitude=latitude, longitude=longitude, uuid=uuid)
    _atualizar_confirmacao_vuupt(conn, rota, "CONFIRMADO", None)
    conn.commit()
    return montar_rota(conn, conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone())


def recusar_rota(conn: sqlite3.Connection, rota_id: int, agent_id: int, motivo: str | None,
                 ocorrido_em: str | None = None, uuid: str | None = None) -> dict:
    rota = rota_do_motorista(conn, rota_id, agent_id)
    if rota["status"] not in (banco.ROTA_PLANEJADA, banco.ROTA_ACEITA):
        raise OperacaoInvalida(f"Rota já está {rota['status'].lower()} -- não dá mais pra recusar.", 409)
    motivo = (motivo or "").strip()[:500] or None
    conn.execute("UPDATE nucleo_rotas SET status = ?, atualizado_em = ? WHERE id = ?",
                 (banco.ROTA_PLANEJADA, banco.agora(), rota_id))
    registrar_evento(conn, "ROTA_RECUSADA", banco.ORIGEM_APP, ocorrido_em, rota_id=rota_id, agent_id=agent_id,
                     dados={"motivo": motivo}, uuid=uuid)
    _atualizar_confirmacao_vuupt(conn, rota, "RECUSADO", motivo)
    conn.commit()
    return montar_rota(conn, conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone())


def iniciar_rota(conn: sqlite3.Connection, rota_id: int, agent_id: int, ocorrido_em: str | None = None,
                 latitude=None, longitude=None, uuid: str | None = None) -> dict:
    rota = rota_do_motorista(conn, rota_id, agent_id)
    _exigir_provedor_app(rota)
    if rota["status"] == banco.ROTA_EM_ROTA:
        return montar_rota(conn, rota)
    if rota["status"] not in (banco.ROTA_PLANEJADA, banco.ROTA_ACEITA):
        raise OperacaoInvalida(f"Rota está {rota['status'].lower()}.", 409)
    agora = banco.agora()
    conn.execute("UPDATE nucleo_rotas SET status = ?, iniciada_em = COALESCE(iniciada_em, ?), atualizado_em = ? WHERE id = ?",
                 (banco.ROTA_EM_ROTA, ocorrido_em or agora, agora, rota_id))
    registrar_evento(conn, "ROTA_EM_ROTA", banco.ORIGEM_APP, ocorrido_em, rota_id=rota_id, agent_id=agent_id,
                     latitude=latitude, longitude=longitude, uuid=uuid)
    conn.commit()
    return montar_rota(conn, conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone())


def _recalcular_contadores(conn: sqlite3.Connection, rota_id: int) -> dict:
    row = conn.execute("""
        SELECT COUNT(*) FILTER (WHERE situacao != 'CANCELADA') AS total,
               COUNT(*) FILTER (WHERE situacao IN ('ENTREGUE', 'PARCIAL')) AS entregues,
               COUNT(*) FILTER (WHERE situacao = 'INSUCESSO') AS insucessos,
               COUNT(*) FILTER (WHERE situacao IN ('PENDENTE', 'EM_ROTA')) AS pendentes
        FROM nucleo_paradas WHERE rota_id = ?
    """, (rota_id,)).fetchone()
    conn.execute("UPDATE nucleo_rotas SET total_paradas = ?, entregues = ?, insucessos = ?, atualizado_em = ? WHERE id = ?",
                 (row["total"], row["entregues"], row["insucessos"], banco.agora(), rota_id))
    return dict(row)


def registrar_evento_parada(conn: sqlite3.Connection, parada_id: int, agent_id: int, dados: dict) -> dict:
    """
    dados: {uuid, tipo, ocorrido_em?, latitude?, longitude?, precisao_m?,
            motivo_id?, observacoes?, checklist?{...}}
    Idempotente por uuid. Devolve {"ja_registrado", "parada", "rota_status"}.
    """
    p = conn.execute("SELECT * FROM nucleo_paradas WHERE id = ?", (parada_id,)).fetchone()
    if not p:
        raise OperacaoInvalida("Parada não encontrada.", 404)
    rota = rota_do_motorista(conn, p["rota_id"], agent_id)
    _exigir_provedor_app(rota)
    tipo = str(dados.get("tipo") or "").upper()
    if tipo not in TIPOS_EVENTO_PARADA:
        raise OperacaoInvalida(f"Tipo de evento inválido: {tipo!r}.")
    uuid = dados.get("uuid")
    if not uuid:
        raise OperacaoInvalida("Evento sem uuid (gerado no aparelho) -- obrigatório pra fila offline.")
    if rota["status"] in (banco.ROTA_CANCELADA,):
        raise OperacaoInvalida("Rota cancelada.", 409)
    if p["situacao"] == banco.PARADA_CANCELADA:
        raise OperacaoInvalida("Parada cancelada nesta rota.", 409)

    ja = conn.execute("SELECT id FROM nucleo_eventos WHERE uuid = ?", (uuid,)).fetchone()
    if ja:
        return {"ja_registrado": True, "parada": montar_parada(conn, p), "rota_status": rota["status"]}

    motivo_id = dados.get("motivo_id")
    motivo_texto = None
    if tipo in ("INSUCESSO", "PARCIAL"):
        if motivo_id is None:
            raise OperacaoInvalida("Insucesso/parcial exige motivo_id.")
        if _tem_tabela(conn, "motivos_ocorrencia"):
            m = conn.execute("SELECT motivo_texto FROM motivos_ocorrencia WHERE id = ?", (motivo_id,)).fetchone()
            if not m:
                raise OperacaoInvalida("motivo_id desconhecido.")
            motivo_texto = m["motivo_texto"]

    ocorrido_em = dados.get("ocorrido_em") or banco.agora()
    agora = banco.agora()

    # Rota que ainda não foi iniciada explicitamente entra EM_ROTA no 1º evento
    if rota["status"] in (banco.ROTA_PLANEJADA, banco.ROTA_ACEITA):
        conn.execute("UPDATE nucleo_rotas SET status = ?, iniciada_em = COALESCE(iniciada_em, ?), atualizado_em = ? WHERE id = ?",
                     (banco.ROTA_EM_ROTA, ocorrido_em, agora, rota["id"]))

    if tipo == "CHEGADA":
        conn.execute("UPDATE nucleo_paradas SET arrived_at = COALESCE(arrived_at, ?), situacao = CASE WHEN situacao = 'PENDENTE' THEN 'EM_ROTA' ELSE situacao END, atualizado_em = ? WHERE id = ?",
                     (ocorrido_em, agora, parada_id))
    elif tipo in _SITUACAO_DO_EVENTO:
        situacao = _SITUACAO_DO_EVENTO[tipo]
        conn.execute("""
            UPDATE nucleo_paradas SET situacao = ?, completed_at = ?, motivo_id = ?, motivo_texto = ?,
                   arrived_at = COALESCE(arrived_at, ?), atualizado_em = ?
            WHERE id = ?
        """, (situacao, ocorrido_em, motivo_id, motivo_texto, ocorrido_em, agora, parada_id))
        if p["codigo"]:
            nucleo_pedidos.upsert_pedido(conn, p["codigo"], {}, origem=banco.ORIGEM_APP,
                                         status=_STATUS_PEDIDO_DA_SITUACAO[situacao])

    registrar_evento(
        conn, tipo, banco.ORIGEM_APP, ocorrido_em, rota_id=rota["id"], parada_id=parada_id, agent_id=agent_id,
        latitude=dados.get("latitude"), longitude=dados.get("longitude"), precisao_m=dados.get("precisao_m"),
        dados={"checklist": dados.get("checklist"), "motivo_id": motivo_id, "motivo_texto": motivo_texto,
               "observacoes": dados.get("observacoes"), "codigo": p["codigo"]},
        uuid=uuid,
    )

    contadores = _recalcular_contadores(conn, rota["id"])
    status_rota = conn.execute("SELECT status FROM nucleo_rotas WHERE id = ?", (rota["id"],)).fetchone()["status"]
    if contadores["pendentes"] == 0 and status_rota == banco.ROTA_EM_ROTA:
        status_rota = _concluir(conn, rota["id"], agent_id, ocorrido_em)
    conn.commit()
    p_novo = conn.execute("SELECT * FROM nucleo_paradas WHERE id = ?", (parada_id,)).fetchone()
    return {"ja_registrado": False, "parada": montar_parada(conn, p_novo), "rota_status": status_rota,
            "contadores": contadores}


def _concluir(conn: sqlite3.Connection, rota_id: int, agent_id: int, ocorrido_em: str,
              km_informado: float | None = None, dados_extra: dict | None = None) -> str:
    km_gps = calcular_km_gps(conn, rota_id)
    if km_gps is not None:
        km, fonte = km_gps, "GPS_APP"
    elif km_informado is not None:
        km, fonte = float(km_informado), "INFORMADO"
    else:
        km, fonte = None, None
    conn.execute("""
        UPDATE nucleo_rotas SET status = ?, concluida_em = COALESCE(concluida_em, ?),
               km_real = COALESCE(?, km_real), km_fonte = COALESCE(?, km_fonte), atualizado_em = ?
        WHERE id = ?
    """, (banco.ROTA_CONCLUIDA, ocorrido_em, km, fonte, banco.agora(), rota_id))
    registrar_evento(conn, "ROTA_CONCLUIDA", banco.ORIGEM_APP, ocorrido_em, rota_id=rota_id, agent_id=agent_id,
                     dados={"km_gps": km_gps, "km_informado": km_informado, **(dados_extra or {})})
    return banco.ROTA_CONCLUIDA


def finalizar_rota(conn: sqlite3.Connection, rota_id: int, agent_id: int, km_informado: float | None = None,
                   pedagio: float | None = None, observacoes: str | None = None,
                   ocorrido_em: str | None = None, uuid: str | None = None) -> dict:
    rota = rota_do_motorista(conn, rota_id, agent_id)
    _exigir_provedor_app(rota)
    if rota["status"] == banco.ROTA_CONCLUIDA:
        return montar_rota(conn, rota)
    if rota["status"] != banco.ROTA_EM_ROTA:
        raise OperacaoInvalida("Rota ainda não foi iniciada.", 409)
    contadores = _recalcular_contadores(conn, rota_id)
    if contadores["pendentes"] > 0:
        raise OperacaoInvalida(f"Ainda há {contadores['pendentes']} parada(s) sem resultado.", 409)
    if uuid and conn.execute("SELECT 1 FROM nucleo_eventos WHERE uuid = ?", (uuid,)).fetchone():
        return montar_rota(conn, rota)
    _concluir(conn, rota_id, agent_id, ocorrido_em or banco.agora(), km_informado,
              {"pedagio": pedagio, "observacoes": observacoes, "uuid": uuid})
    conn.commit()
    return montar_rota(conn, conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone())


# ── Comprovantes ───────────────────────────────────────────────────────────────

def registrar_comprovante(conn: sqlite3.Connection, parada_id: int, agent_id: int, tipo: str, uuid: str,
                          caminho_local: str | None, sha256: str | None, tamanho_bytes: int | None,
                          capturado_em: str | None, caminho_gcs: str | None = None) -> dict:
    p = conn.execute("SELECT * FROM nucleo_paradas WHERE id = ?", (parada_id,)).fetchone()
    if not p:
        raise OperacaoInvalida("Parada não encontrada.", 404)
    rota = rota_do_motorista(conn, p["rota_id"], agent_id)
    _exigir_provedor_app(rota)
    tipo = str(tipo or "").upper()
    if tipo not in TIPOS_COMPROVANTE:
        raise OperacaoInvalida(f"Tipo de comprovante inválido: {tipo!r}.")
    if not uuid:
        raise OperacaoInvalida("Comprovante sem uuid.")
    ja = conn.execute("SELECT id FROM nucleo_comprovantes WHERE uuid = ?", (uuid,)).fetchone()
    if ja:
        return {"id": ja["id"], "ja_registrado": True}
    cur = conn.execute("""
        INSERT INTO nucleo_comprovantes (uuid, rota_id, parada_id, tipo, caminho_gcs, caminho_local, sha256,
                                         tamanho_bytes, capturado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (uuid, rota["id"], parada_id, tipo, caminho_gcs, caminho_local, sha256, tamanho_bytes, capturado_em))
    registrar_evento(conn, "FOTO", banco.ORIGEM_APP, capturado_em, rota_id=rota["id"], parada_id=parada_id,
                     agent_id=agent_id, dados={"tipo": tipo, "comprovante_id": cur.lastrowid, "sha256": sha256})
    conn.commit()
    return {"id": cur.lastrowid, "ja_registrado": False}


# ── GPS / km ───────────────────────────────────────────────────────────────────

def registrar_gps(conn: sqlite3.Connection, agent_id: int, pontos: list[dict]) -> int:
    """Lote de pontos {uuid, rota_id, ocorrido_em, latitude, longitude, precisao_m}."""
    novos = 0
    for pt in pontos or []:
        try:
            lat, lng = float(pt["latitude"]), float(pt["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        rota_id = pt.get("rota_id")
        if rota_id is not None:
            try:
                rota_do_motorista(conn, int(rota_id), agent_id)
            except OperacaoInvalida:
                continue
        ev = registrar_evento(conn, "GPS", banco.ORIGEM_APP, pt.get("ocorrido_em"), rota_id=rota_id,
                              agent_id=agent_id, latitude=lat, longitude=lng, precisao_m=pt.get("precisao_m"),
                              uuid=pt.get("uuid"))
        if ev:
            novos += 1
    conn.commit()
    return novos


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def calcular_km_gps(conn: sqlite3.Connection, rota_id: int) -> float | None:
    """Soma dos trechos entre pontos GPS da rota (ordem cronológica), com
    filtro de precisão e de salto. None se não houver ao menos 2 pontos."""
    rows = conn.execute("""
        SELECT latitude, longitude, precisao_m FROM nucleo_eventos
        WHERE rota_id = ? AND latitude IS NOT NULL AND longitude IS NOT NULL
          AND tipo IN ('GPS', 'CHEGADA', 'ENTREGUE', 'PARCIAL', 'INSUCESSO', 'ROTA_EM_ROTA', 'ROTA_CONCLUIDA')
        ORDER BY ocorrido_em, id
    """, (rota_id,)).fetchall()
    pontos = [(r[0], r[1]) for r in rows if r[2] is None or float(r[2]) <= GPS_PRECISAO_MAX_M]
    if len(pontos) < 2:
        return None
    total = 0.0
    for (a, b) in zip(pontos, pontos[1:]):
        trecho = _haversine_km(a[0], a[1], b[0], b[1])
        if trecho <= GPS_SALTO_MAX_KM:
            total += trecho
    return round(total, 2)


# ── Checklist / motivos ────────────────────────────────────────────────────────

def carregar_checklist(conn: sqlite3.Connection) -> dict:
    """Modelo do checklist por fluxo (checklist_modelo, desenho de junho)
    + catálogo de motivos (motivos_ocorrencia). Tabelas ausentes → listas
    vazias, o app usa o fallback embutido."""
    fluxos: dict[str, list[dict]] = {}
    if _tem_tabela(conn, "checklist_modelo"):
        for r in conn.execute("SELECT fluxo, chave, rotulo, tipo_campo, obrigatorio, opcoes, aviso, ordem FROM checklist_modelo WHERE ativo = 1 ORDER BY fluxo, ordem"):
            fluxos.setdefault(r["fluxo"], []).append({
                "chave": r["chave"], "rotulo": r["rotulo"], "tipo": r["tipo_campo"],
                "obrigatorio": bool(r["obrigatorio"]),
                "opcoes": json.loads(r["opcoes"]) if r["opcoes"] else None, "aviso": r["aviso"],
            })
    motivos = []
    if _tem_tabela(conn, "motivos_ocorrencia"):
        motivos = [dict(r) for r in conn.execute(
            "SELECT id, motivo_texto, categoria FROM motivos_ocorrencia WHERE vuupt_failed_reason_id IS NOT NULL OR id < 100 ORDER BY motivo_texto")]
    return {"fluxos": fluxos, "motivos": motivos}


# ── Ofertas (marketplace) ──────────────────────────────────────────────────────

def _elegivel(oferta: sqlite3.Row, agent_id: int) -> bool:
    try:
        lista = json.loads(oferta["agent_ids_elegiveis"] or "[]")
    except (TypeError, ValueError):
        return False
    for e in lista:
        aid = e.get("agent_id") if isinstance(e, dict) else e
        if aid == agent_id:
            return True
    return False


def listar_ofertas(conn: sqlite3.Connection, agent_id: int | None) -> dict:
    if agent_id is None or not _tem_tabela(conn, "ofertas_rota"):
        return {"abertas": [], "minhas": []}
    hoje = date.today().isoformat()
    abertas, minhas = [], []
    for o in conn.execute("SELECT * FROM ofertas_rota WHERE data_alvo >= ? ORDER BY data_alvo, id", (hoje,)):
        item = {"rascunho_id": o["rascunho_id"], "data_alvo": o["data_alvo"],
                "resumo": json.loads(o["resumo_json"] or "{}"), "status": o["status"]}
        if o["status"] == "ABERTA" and _elegivel(o, agent_id):
            abertas.append(item)
        elif o["status"] == "ESCOLHIDA" and o["escolhido_por"] == agent_id:
            item["aplicada"] = o["aplicado_em"] is not None
            minhas.append(item)
    return {"abertas": abertas, "minhas": minhas}


def escolher_oferta(conn: sqlite3.Connection, agent_id: int | None, rascunho_id: int) -> dict:
    """Claim atômico (UPDATE ... WHERE status='ABERTA'), mesmo padrão de
    confirmacao_motoristas/app.py::_tentar_escolher. sincronizado_em=NULL
    pra a escolha ser empurrada pra página pública também."""
    if agent_id is None:
        raise OperacaoInvalida("Motorista sem agent_id -- não pode escolher rota.", 403)
    o = conn.execute("SELECT * FROM ofertas_rota WHERE rascunho_id = ?", (rascunho_id,)).fetchone()
    if not o or not _elegivel(o, agent_id):
        raise OperacaoInvalida("Essa rota não está disponível pra você.", 404)
    cur = conn.execute("""
        UPDATE ofertas_rota SET status = 'ESCOLHIDA', escolhido_por = ?, escolhido_em = ?, sincronizado_em = NULL
        WHERE rascunho_id = ? AND status = 'ABERTA'
    """, (agent_id, banco.agora(), rascunho_id))
    if cur.rowcount == 0:
        conn.commit()
        raise OperacaoInvalida("Essa rota não está mais disponível -- outro motorista já escolheu (ou ela foi retirada).", 409)
    registrar_evento(conn, "OFERTA_ESCOLHIDA", banco.ORIGEM_APP, agent_id=agent_id, dados={"rascunho_id": rascunho_id})
    conn.commit()
    return listar_ofertas(conn, agent_id)


def cancelar_escolha_oferta(conn: sqlite3.Connection, agent_id: int | None, rascunho_id: int) -> dict:
    cur = conn.execute("""
        UPDATE ofertas_rota SET status = 'ABERTA', escolhido_por = NULL, escolhido_em = NULL, sincronizado_em = NULL
        WHERE rascunho_id = ? AND status = 'ESCOLHIDA' AND escolhido_por = ? AND aplicado_em IS NULL
    """, (rascunho_id, agent_id))
    if cur.rowcount == 0:
        conn.commit()
        raise OperacaoInvalida("Essa escolha já foi aplicada no planejamento -- fale com a operação pra desfazer.", 409)
    registrar_evento(conn, "OFERTA_CANCELADA", banco.ORIGEM_APP, agent_id=agent_id, dados={"rascunho_id": rascunho_id})
    conn.commit()
    return listar_ofertas(conn, agent_id)


# ── Disponibilidade ────────────────────────────────────────────────────────────

def listar_disponibilidade(conn: sqlite3.Connection, agent_id: int | None, de: date, ate: date) -> list[dict]:
    if agent_id is None or not _tem_tabela(conn, "disponibilidade_motoristas"):
        return []
    rows = conn.execute("""
        SELECT data, disponivel, motivo FROM disponibilidade_motoristas
        WHERE agent_id = ? AND data BETWEEN ? AND ? ORDER BY data
    """, (agent_id, de.isoformat(), ate.isoformat())).fetchall()
    return [{"data": r["data"], "disponivel": bool(r["disponivel"]), "motivo": r["motivo"]} for r in rows]


def definir_disponibilidade(conn: sqlite3.Connection, agent_id: int | None, data_ini: date, data_fim: date,
                            disponivel: bool | None, motivo: str | None) -> int:
    """disponivel None = remove o ajuste (volta ao padrão semanal). Mesma
    tabela/regra de regras/disponibilidade_motoristas.py."""
    if agent_id is None:
        raise OperacaoInvalida("Motorista sem agent_id.", 403)
    if data_fim < data_ini:
        data_ini, data_fim = data_fim, data_ini
    if (data_fim - data_ini).days > 90:
        raise OperacaoInvalida("Período máximo de 90 dias.")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS disponibilidade_motoristas (
            agent_id INTEGER NOT NULL, data TEXT NOT NULL, disponivel INTEGER NOT NULL,
            motivo TEXT, atualizado_em TEXT NOT NULL, PRIMARY KEY (agent_id, data))
    """)
    agora = banco.agora()
    n = 0
    dia = data_ini
    while dia <= data_fim:
        if disponivel is None:
            conn.execute("DELETE FROM disponibilidade_motoristas WHERE agent_id = ? AND data = ?", (agent_id, dia.isoformat()))
        else:
            conn.execute("""
                INSERT INTO disponibilidade_motoristas (agent_id, data, disponivel, motivo, atualizado_em)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, data) DO UPDATE SET disponivel = excluded.disponivel,
                    motivo = excluded.motivo, atualizado_em = excluded.atualizado_em
            """, (agent_id, dia.isoformat(), int(bool(disponivel)), (motivo or "").strip()[:200] or None, agora))
        n += 1
        dia += timedelta(days=1)
    registrar_evento(conn, "DISPONIBILIDADE", banco.ORIGEM_APP, agent_id=agent_id,
                     dados={"de": data_ini.isoformat(), "ate": data_fim.isoformat(), "disponivel": disponivel, "motivo": motivo})
    conn.commit()
    return n
