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
from datetime import date, datetime, timedelta, timezone

from nucleo import banco, pedidos as nucleo_pedidos, tempos, validacao_fotos
from nucleo.rotas import registrar_evento

# Passos da parada no app (Hugo, 26/08): DESLOCAMENTO ("Iniciar deslocamento",
# grava started_at) -> CHEGADA ("Cheguei no local", grava arrived_at) ->
# ENTREGUE | PARCIAL | INSUCESSO (completed_at). Tempo de deslocamento e
# tempo no local saem direto desses três timestamps.
# REAGENDAR (Hugo, 26/08): motorista esteve no local e marca um retorno
# (novo_horario "YYYY-MM-DD HH:MM" ou "FIM" = depois das outras paradas).
# A parada volta a PENDENTE com reagendado_para preenchido e tentativas+1;
# a tentativa (started/arrived/tempo no local) fica registrada no evento.
TIPOS_EVENTO_PARADA = {"DESLOCAMENTO", "CHEGADA", "ENTREGUE", "PARCIAL", "INSUCESSO", "REAGENDAR", "OBSERVACAO"}
REAGENDAR_FIM = "FIM"
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


def _ler_json(v) -> dict:
    try:
        d = json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


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
    d["pedagios"] = listar_pedagios(conn, rota["id"])
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
        {"id": c["id"], "tipo": c["tipo"], "uuid": c["uuid"], "capturado_em": c["capturado_em"],
         "validacao": c["resultado_validacao"]}
        for c in conn.execute("SELECT id, tipo, uuid, capturado_em, resultado_validacao FROM nucleo_comprovantes "
                              "WHERE parada_id = ? ORDER BY id", (p["id"],))
    ]
    # NFs do pedido: o app pede UM CANHOTO POR NF (Hugo, 12/09). Lista
    # vazia = pedido sem NF conhecida (Fruta Fina/placeholder da Stokki),
    # aí é um canhoto só, sem cobrança de número.
    d["nfs"] = validacao_fotos.nfs_do_pedido(conn, p["codigo"])
    return d


# ── Pedágio (Hugo, 11/09: reembolso à parte, foto + valor, aprovação no painel) ──

def listar_pedagios(conn: sqlite3.Connection, rota_id: int) -> list[dict]:
    return [
        {"id": r["id"], "uuid": r["uuid"], "valor_informado": r["valor_informado"], "status": r["status"],
         "tipo": r["tipo"] or banco.DESPESA_PEDAGIO, "tipo_rotulo": banco.TIPOS_DESPESA.get(r["tipo"] or banco.DESPESA_PEDAGIO, r["tipo"]),
         "descricao": r["descricao"], "parada_id": r["parada_id"], "pedido_codigo": r["pedido_codigo"],
         "pedido_nome": r["pedido_nome"],
         "valor_aprovado": r["valor_aprovado"], "capturado_em": r["capturado_em"], "enviado_em": r["enviado_em"],
         "observacao_revisao": r["observacao_revisao"], "tem_foto": bool(r["caminho_local"] or r["caminho_gcs"]),
         "validacao": (_ler_json(r["dados_json"]).get("validacao") or {}).get("resultado")}
        for r in conn.execute("""
            SELECT p.*, pa.codigo AS pedido_codigo, pa.destinatario_nome AS pedido_nome
            FROM nucleo_pedagios p LEFT JOIN nucleo_paradas pa ON pa.id = p.parada_id
            WHERE p.rota_id = ? ORDER BY p.id
        """, (rota_id,))
    ]


def registrar_pedagio(conn: sqlite3.Connection, rota_id: int, agent_id: int, uuid: str, valor: float | None,
                      caminho_local: str | None, sha256: str | None, tamanho_bytes: int | None,
                      capturado_em: str | None, caminho_gcs: str | None = None,
                      validacao: dict | None = None, tipo: str | None = None, descricao: str | None = None,
                      parada_id: int | str | None = None) -> dict:
    """Um comprovante de pedágio da rota (o motorista pode mandar vários).
    Aceito em rota APP EM_ROTA ou CONCLUIDA (ele fecha a rota e depois
    fotografa os recibos, ou manda no caminho). Entra como PENDENTE; só
    o painel aprova (revisar_pedagio).

    `tipo` (Hugo, 14/09): PEDAGIO (padrão, app antigo não manda) ou despesa
    adicional ESTACIONAMENTO / DESCARGA / OUTROS -- mesmo fluxo; OUTROS
    exige `descricao`; ESTACIONAMENTO e DESCARGA exigem `parada_id` (pedido
    de referência, parada desta rota)."""
    rota = rota_do_motorista(conn, rota_id, agent_id)
    _exigir_provedor_app(rota)
    if rota["status"] not in (banco.ROTA_EM_ROTA, banco.ROTA_CONCLUIDA):
        raise OperacaoInvalida("Pedágio e despesas só podem ser informados com a rota em andamento ou concluída.", 409)
    tipo = (tipo or banco.DESPESA_PEDAGIO).strip().upper()
    if tipo not in banco.TIPOS_DESPESA:
        raise OperacaoInvalida("Tipo de despesa inválido.")
    descricao = (descricao or "").strip()[:200] or None
    if tipo == banco.DESPESA_OUTROS and not descricao:
        raise OperacaoInvalida("Descreva a despesa (tipo Outros).")
    if parada_id not in (None, ""):
        try:
            parada_id = int(parada_id)
        except (TypeError, ValueError):
            raise OperacaoInvalida("Pedido de referência inválido.")
        if not conn.execute("SELECT 1 FROM nucleo_paradas WHERE id = ? AND rota_id = ?", (parada_id, rota_id)).fetchone():
            raise OperacaoInvalida("O pedido de referência não é desta rota.")
    else:
        parada_id = None
    if tipo in banco.DESPESAS_COM_PEDIDO and parada_id is None:
        raise OperacaoInvalida(f"Informe o pedido de referência ({banco.TIPOS_DESPESA[tipo]}).")
    if not uuid:
        raise OperacaoInvalida("Pedágio sem uuid.")
    try:
        valor_f = round(float(str(valor).replace(",", ".")), 2)
    except (TypeError, ValueError):
        raise OperacaoInvalida("Informe o valor.")
    if valor_f <= 0 or valor_f > 2000:
        raise OperacaoInvalida("Valor de pedágio fora do esperado (entre R$ 0,01 e R$ 2.000,00).")
    ja = conn.execute("SELECT id FROM nucleo_pedagios WHERE uuid = ?", (uuid,)).fetchone()
    if ja:
        return {"id": ja["id"], "ja_registrado": True}
    cur = conn.execute("""
        INSERT INTO nucleo_pedagios (uuid, rota_id, agent_id, valor_informado, tipo, descricao, parada_id, caminho_local,
                                     caminho_gcs, sha256, tamanho_bytes, capturado_em, status, dados_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (uuid, rota_id, agent_id, valor_f, tipo, descricao, parada_id, caminho_local, caminho_gcs, sha256, tamanho_bytes,
          capturado_em, banco.PEDAGIO_PENDENTE,
          _json({"validacao": validacao}) if validacao else None))
    registrar_evento(conn, "PEDAGIO", banco.ORIGEM_APP, capturado_em, rota_id=rota_id, agent_id=agent_id,
                     dados={"pedagio_id": cur.lastrowid, "valor": valor_f, "tipo": tipo, "sha256": sha256})
    conn.commit()
    return {"id": cur.lastrowid, "ja_registrado": False}


def cancelar_pedagio(conn: sqlite3.Connection, rota_id: int, pedagio_id: int, agent_id: int) -> list[dict]:
    """Motorista desiste de um pedágio que ele mesmo mandou (errou o valor,
    foto errada, mandou duas vezes). Só enquanto PENDENTE: aprovado já está
    no extrato e rejeitado já foi decidido pelo painel. Cancelar de novo é
    idempotente. Devolve a lista de pedágios da rota."""
    rota_do_motorista(conn, rota_id, agent_id)
    row = conn.execute("SELECT * FROM nucleo_pedagios WHERE id = ? AND rota_id = ?", (pedagio_id, rota_id)).fetchone()
    if not row or row["agent_id"] != agent_id:
        raise OperacaoInvalida("Pedágio não encontrado.", 404)
    if row["status"] == banco.PEDAGIO_CANCELADO:
        return listar_pedagios(conn, rota_id)
    if row["status"] != banco.PEDAGIO_PENDENTE:
        raise OperacaoInvalida("Esse pedágio já foi revisado pelo escritório e não pode mais ser cancelado.", 409)
    agora = banco.agora()
    conn.execute("""
        UPDATE nucleo_pedagios SET status = ?, valor_aprovado = NULL, revisado_em = ?, revisado_por = ?, observacao_revisao = ?
        WHERE id = ?
    """, (banco.PEDAGIO_CANCELADO, agora, "MOTORISTA", "Cancelado pelo motorista no app", pedagio_id))
    registrar_evento(conn, "PEDAGIO_CANCELADO", banco.ORIGEM_APP, agora, rota_id=rota_id, agent_id=agent_id,
                     dados={"pedagio_id": pedagio_id, "valor": row["valor_informado"]})
    conn.commit()
    return listar_pedagios(conn, rota_id)


def revisar_pedagio(conn: sqlite3.Connection, pedagio_id: int, status: str, revisor: str,
                    valor_aprovado: float | None = None, observacao: str | None = None) -> dict:
    """Painel: aprova (com o valor que vale, por padrão o informado) ou
    rejeita. Pode ser refeito (reabrir = PENDENTE)."""
    status = str(status or "").upper()
    if status not in (banco.PEDAGIO_APROVADO, banco.PEDAGIO_REJEITADO, banco.PEDAGIO_PENDENTE):
        raise OperacaoInvalida(f"Status de pedágio inválido: {status!r}.")
    row = conn.execute("SELECT * FROM nucleo_pedagios WHERE id = ?", (pedagio_id,)).fetchone()
    if not row:
        raise OperacaoInvalida("Pedágio não encontrado.", 404)
    aprovado = None
    if status == banco.PEDAGIO_APROVADO:
        aprovado = float(row["valor_informado"]) if valor_aprovado is None else round(float(valor_aprovado), 2)
        if aprovado < 0:
            raise OperacaoInvalida("Valor aprovado não pode ser negativo.")
    agora = banco.agora()
    conn.execute("""
        UPDATE nucleo_pedagios SET status = ?, valor_aprovado = ?, revisado_em = ?, revisado_por = ?, observacao_revisao = ?
        WHERE id = ?
    """, (status, aprovado, None if status == banco.PEDAGIO_PENDENTE else agora,
          None if status == banco.PEDAGIO_PENDENTE else revisor, observacao, pedagio_id))
    registrar_evento(conn, "PEDAGIO_REVISADO", banco.ORIGEM_PAINEL, agora, rota_id=row["rota_id"], agent_id=row["agent_id"],
                     dados={"pedagio_id": pedagio_id, "status": status, "valor_aprovado": aprovado, "revisor": revisor})
    conn.commit()
    return dict(conn.execute("SELECT * FROM nucleo_pedagios WHERE id = ?", (pedagio_id,)).fetchone())


def _filtro_pedagios_painel(status: str | None, data_inicio: str | None, data_fim: str | None,
                            agent_id: int | None) -> tuple[str, list]:
    """WHERE compartilhado entre a lista e as contagens das abas do painel,
    pra que as duas sempre concordem com os mesmos filtros. As datas são
    da ROTA (r.data_rota, ISO), não do envio -- é assim que o Hugo fecha
    o extrato do motorista."""
    clausulas, params = [], []
    if status:
        clausulas.append("p.status = ?")
        params.append(status)
    if data_inicio:
        clausulas.append("r.data_rota >= ?")
        params.append(data_inicio)
    if data_fim:
        clausulas.append("r.data_rota <= ?")
        params.append(data_fim)
    if agent_id is not None:
        clausulas.append("p.agent_id = ?")
        params.append(int(agent_id))
    return (" WHERE " + " AND ".join(clausulas)) if clausulas else "", params


def listar_pedagios_painel(conn: sqlite3.Connection, status: str | None = banco.PEDAGIO_PENDENTE,
                           limite: int = 300, data_inicio: str | None = None, data_fim: str | None = None,
                           agent_id: int | None = None) -> list[dict]:
    """Fila de revisão do painel: pedágios (por padrão só PENDENTES) com
    rota e motorista, mais recentes primeiro. Filtros opcionais por período
    da rota (ISO, inclusive) e motorista (agent_id)."""
    where, params = _filtro_pedagios_painel(status, data_inicio, data_fim, agent_id)
    sql = f"""
        SELECT p.*, r.data_rota, r.nome AS rota_nome, r.motorista_nome, r.status AS rota_status,
               pa.codigo AS pedido_codigo, pa.destinatario_nome AS pedido_nome
        FROM nucleo_pedagios p JOIN nucleo_rotas r ON r.id = p.rota_id
        LEFT JOIN nucleo_paradas pa ON pa.id = p.parada_id
        {where} ORDER BY r.data_rota DESC, p.id DESC LIMIT ?
    """
    saida = []
    for r in conn.execute(sql, params + [limite]).fetchall():
        d = dict(r)
        # Veredito da conferência automática do recibo, quando houver
        # (nucleo/validacao_fotos.py) -- o painel mostra junto do valor.
        d["validacao"] = _ler_json(d.get("dados_json")).get("validacao") or {}
        saida.append(d)
    return saida


def contar_pedagios_painel(conn: sqlite3.Connection, data_inicio: str | None = None, data_fim: str | None = None,
                           agent_id: int | None = None) -> dict[str, int]:
    """Quantos pedágios por status dentro dos mesmos filtros da lista
    (números das abas Pendentes/Aprovados/...)."""
    where, params = _filtro_pedagios_painel(None, data_inicio, data_fim, agent_id)
    sql = f"""
        SELECT p.status, COUNT(*) FROM nucleo_pedagios p JOIN nucleo_rotas r ON r.id = p.rota_id
        {where} GROUP BY p.status
    """
    return {r[0]: r[1] for r in conn.execute(sql, params).fetchall()}


def motoristas_com_pedagio(conn: sqlite3.Connection) -> list[dict]:
    """Opções do filtro de motorista do painel: só quem já mandou algum
    pedágio, com o nome mais recente que apareceu na rota."""
    rows = conn.execute("""
        SELECT p.agent_id, r.motorista_nome
        FROM nucleo_pedagios p JOIN nucleo_rotas r ON r.id = p.rota_id
        WHERE p.agent_id IS NOT NULL
        ORDER BY p.id DESC
    """).fetchall()
    vistos: dict[int, str] = {}
    for r in rows:
        vistos.setdefault(r["agent_id"], r["motorista_nome"] or f"Agente {r['agent_id']}")
    return sorted(({"agent_id": k, "nome": v} for k, v in vistos.items()), key=lambda m: m["nome"].lower())


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
               COUNT(*) FILTER (WHERE situacao IN ('PENDENTE', 'EM_DESLOCAMENTO', 'EM_ROTA')) AS pendentes
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

    novo_horario = None
    if tipo == "REAGENDAR":
        novo_horario = str(dados.get("novo_horario") or "").strip()
        if novo_horario.upper() == REAGENDAR_FIM:
            novo_horario = REAGENDAR_FIM
        elif not tempos.parse_ts(novo_horario):
            raise OperacaoInvalida("Reagendar exige novo_horario ('YYYY-MM-DD HH:MM' ou 'FIM').")

    ocorrido_em = dados.get("ocorrido_em") or banco.agora()
    agora = banco.agora()

    # Rota que ainda não foi iniciada explicitamente entra EM_ROTA no 1º evento
    if rota["status"] in (banco.ROTA_PLANEJADA, banco.ROTA_ACEITA):
        conn.execute("UPDATE nucleo_rotas SET status = ?, iniciada_em = COALESCE(iniciada_em, ?), atualizado_em = ? WHERE id = ?",
                     (banco.ROTA_EM_ROTA, ocorrido_em, agora, rota["id"]))

    if tipo == "DESLOCAMENTO":
        conn.execute("""
            UPDATE nucleo_paradas SET started_at = COALESCE(started_at, ?),
                   situacao = CASE WHEN situacao = 'PENDENTE' THEN 'EM_DESLOCAMENTO' ELSE situacao END, atualizado_em = ?
            WHERE id = ?
        """, (ocorrido_em, agora, parada_id))
    elif tipo == "CHEGADA":
        conn.execute("""
            UPDATE nucleo_paradas SET arrived_at = COALESCE(arrived_at, ?), started_at = COALESCE(started_at, ?),
                   situacao = CASE WHEN situacao IN ('PENDENTE', 'EM_DESLOCAMENTO') THEN 'EM_ROTA' ELSE situacao END, atualizado_em = ?
            WHERE id = ?
        """, (ocorrido_em, ocorrido_em, agora, parada_id))
    elif tipo == "REAGENDAR":
        # Guarda a tentativa no evento e libera a parada pra nova ida.
        tentativa = {"started_at": p["started_at"], "arrived_at": p["arrived_at"],
                     "tempo_no_local_s": tempos.diferenca_s(p["arrived_at"], ocorrido_em)}
        dados = {**dados, "tentativa": tentativa}
        conn.execute("""
            UPDATE nucleo_paradas SET situacao = 'PENDENTE', started_at = NULL, arrived_at = NULL,
                   tempo_deslocamento_s = NULL, tempo_no_local_s = NULL,
                   reagendado_para = ?, tentativas = tentativas + 1, atualizado_em = ?
            WHERE id = ?
        """, (novo_horario, agora, parada_id))
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

    # Durações (deslocamento / no local) recalculadas a cada passo.
    if tipo != "REAGENDAR":
        tempos.atualizar_tempos_parada(conn, parada_id)

    registrar_evento(
        conn, tipo, banco.ORIGEM_APP, ocorrido_em, rota_id=rota["id"], parada_id=parada_id, agent_id=agent_id,
        latitude=dados.get("latitude"), longitude=dados.get("longitude"), precisao_m=dados.get("precisao_m"),
        dados={"checklist": dados.get("checklist"), "motivo_id": motivo_id, "motivo_texto": motivo_texto,
               "observacoes": dados.get("observacoes"), "codigo": p["codigo"],
               "novo_horario": novo_horario, "tentativa": dados.get("tentativa")},
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
                          capturado_em: str | None, caminho_gcs: str | None = None,
                          validacao: dict | None = None) -> dict:
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
    # Validação automática (nucleo/validacao_fotos.py): preenche as colunas
    # que já existiam vazias desde a Fase A. NAO_VERIFICADO (validação
    # desligada, sem chave, sem sinal) não marca validado_em.
    resultado_v = (validacao or {}).get("resultado")
    conferida = resultado_v in ("APROVADO", "REPROVADO")
    cur = conn.execute("""
        INSERT INTO nucleo_comprovantes (uuid, rota_id, parada_id, tipo, caminho_gcs, caminho_local, sha256,
                                         tamanho_bytes, capturado_em, validado_em, validado_por,
                                         resultado_validacao, dados_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (uuid, rota["id"], parada_id, tipo, caminho_gcs, caminho_local, sha256, tamanho_bytes, capturado_em,
          banco.agora() if conferida else None, "IA" if conferida else None,
          resultado_v, _json(validacao) if validacao else None))
    registrar_evento(conn, "FOTO", banco.ORIGEM_APP, capturado_em, rota_id=rota["id"], parada_id=parada_id,
                     agent_id=agent_id, dados={"tipo": tipo, "comprovante_id": cur.lastrowid, "sha256": sha256,
                                               "validacao": resultado_v})
    conn.commit()
    return {"id": cur.lastrowid, "ja_registrado": False}


def listar_comprovantes_painel(conn: sqlite3.Connection, resultado: str | None = "REPROVADO",
                               limite: int = 300) -> list[dict]:
    """Fila de conferência de canhotos do painel (Hugo, 12/09). Por padrão
    só os REPROVADOS pela validação automática -- é o que precisa de olho
    humano. `resultado=None` traz todos; "PENDENTE" traz os que chegaram
    sem conferência (validação desligada, sem sinal, sem chave)."""
    sql = """
        SELECT c.*, p.codigo, p.destinatario_nome, p.ordem, r.data_rota, r.nome AS rota_nome, r.motorista_nome
        FROM nucleo_comprovantes c
        JOIN nucleo_paradas p ON p.id = c.parada_id
        LEFT JOIN nucleo_rotas r ON r.id = c.rota_id
    """
    params: list = []
    if resultado == "PENDENTE":
        sql += " WHERE c.resultado_validacao IS NULL"
    elif resultado:
        sql += " WHERE c.resultado_validacao = ?"
        params.append(resultado)
    sql += " ORDER BY c.id DESC LIMIT ?"
    params.append(limite)
    saida = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["validacao"] = _ler_json(d.pop("dados_json", None))
        saida.append(d)
    return saida


def revisar_comprovante(conn: sqlite3.Connection, comprovante_id: int, resultado: str, revisor: str,
                        observacao: str | None = None) -> dict:
    """Painel: a palavra final do humano sobre a foto. Sobrescreve o
    veredito da IA (validado_por vira HUMANO) e guarda o que a IA tinha
    dito em `dados_json.ia` -- serve pra medir o acerto do modelo antes
    de confiar mais nele."""
    resultado = str(resultado or "").upper()
    if resultado not in ("APROVADO", "REPROVADO"):
        raise OperacaoInvalida(f"Resultado inválido: {resultado!r} (use APROVADO ou REPROVADO).")
    row = conn.execute("SELECT * FROM nucleo_comprovantes WHERE id = ?", (comprovante_id,)).fetchone()
    if not row:
        raise OperacaoInvalida("Comprovante não encontrado.", 404)
    dados = _ler_json(row["dados_json"])
    if row["validado_por"] == "IA" and "ia" not in dados:
        dados = {"ia": {k: v for k, v in dados.items() if k != "ia"},
                 "ia_resultado": row["resultado_validacao"]}
    dados["revisao"] = {"resultado": resultado, "revisor": revisor, "observacao": observacao, "em": banco.agora()}
    conn.execute("""
        UPDATE nucleo_comprovantes SET resultado_validacao = ?, validado_em = ?, validado_por = 'HUMANO', dados_json = ?
        WHERE id = ?
    """, (resultado, banco.agora(), _json(dados), comprovante_id))
    registrar_evento(conn, "FOTO_REVISADA", banco.ORIGEM_PAINEL, banco.agora(), rota_id=row["rota_id"],
                     parada_id=row["parada_id"],
                     dados={"comprovante_id": comprovante_id, "resultado": resultado, "revisor": revisor})
    conn.commit()
    return dict(conn.execute("SELECT * FROM nucleo_comprovantes WHERE id = ?", (comprovante_id,)).fetchone())


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

def _agora_utc_iso() -> str:
    """Mesmo formato de regras/prioridade_ofertas.FORMATO_UTC -- comparação
    por string com visivel_a_partir_de, em UTC, independente do fuso."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _elegibilidade(oferta: sqlite3.Row, agent_id: int) -> dict | None:
    """Entrada desse agent_id em agent_ids_elegiveis (dict com onda/
    visivel_a_partir_de/vagas_restantes quando a oferta foi publicada
    com ondas -- regras/prioridade_ofertas.py; formato antigo, só o
    id, vira {'agent_id': id}). None = não elegível."""
    try:
        lista = json.loads(oferta["agent_ids_elegiveis"] or "[]")
    except (TypeError, ValueError):
        return None
    for e in lista:
        aid = e.get("agent_id") if isinstance(e, dict) else e
        if aid == agent_id:
            return e if isinstance(e, dict) else {"agent_id": aid}
    return None


def _elegivel(oferta: sqlite3.Row, agent_id: int) -> bool:
    return _elegibilidade(oferta, agent_id) is not None


def _onda_aberta(elegibilidade: dict | None, agora_utc: str) -> bool:
    """Ondas de prioridade: a oferta só aparece/aceita escolha desse
    motorista depois de visivel_a_partir_de (ausente = sempre visível)."""
    if not elegibilidade:
        return False
    visivel = elegibilidade.get("visivel_a_partir_de")
    return not visivel or visivel <= agora_utc


def listar_ofertas(conn: sqlite3.Connection, agent_id: int | None) -> dict:
    if agent_id is None or not _tem_tabela(conn, "ofertas_rota"):
        return {"abertas": [], "minhas": []}
    hoje = date.today().isoformat()
    agora_utc = _agora_utc_iso()
    abertas, minhas = [], []
    for o in conn.execute("SELECT * FROM ofertas_rota WHERE data_alvo >= ? ORDER BY data_alvo, id", (hoje,)):
        item = {"rascunho_id": o["rascunho_id"], "data_alvo": o["data_alvo"],
                "resumo": json.loads(o["resumo_json"] or "{}"), "status": o["status"]}
        if o["status"] == "ABERTA" and _onda_aberta(_elegibilidade(o, agent_id), agora_utc):
            abertas.append(item)
        elif o["status"] == "ESCOLHIDA" and o["escolhido_por"] == agent_id:
            item["aplicada"] = o["aplicado_em"] is not None
            minhas.append(item)
    return {"abertas": abertas, "minhas": minhas}


def escolher_oferta(conn: sqlite3.Connection, agent_id: int | None, rascunho_id: int, marketplace=None) -> dict:
    """Escolha de oferta pelo app.

    Com `marketplace` (nucleo/marketplace_remoto.ClienteMarketplace --
    produção, Hugo 14/09): a disputa acontece na PÁGINA PÚBLICA, que é a
    dona da escolha (mesmo claim atômico de quem escolhe pelo link/CPF);
    só depois de ganhar lá a escolha é espelhada aqui. Antes o app gravava
    só local, a página seguia ABERTA e a reconciliação de 15 em 15 min
    (roteirizacao/sincronizar_respostas_confirmacao.py) desfazia a escolha.

    Sem `marketplace` (dev/testes): claim atômico só no banco local."""
    if agent_id is None:
        raise OperacaoInvalida("Motorista sem agent_id -- não pode escolher rota.", 403)
    o = conn.execute("SELECT * FROM ofertas_rota WHERE rascunho_id = ?", (rascunho_id,)).fetchone()
    elegibilidade = _elegibilidade(o, agent_id) if o else None
    if not elegibilidade:
        raise OperacaoInvalida("Essa rota não está disponível pra você.", 404)
    if not _onda_aberta(elegibilidade, _agora_utc_iso()):
        raise OperacaoInvalida("Essa rota ainda não está liberada pra você -- tente de novo em alguns minutos.", 403)
    # Teto de escolhas por dia (Hugo, 03/09) -- espelho de
    # confirmacao_motoristas/app.py::_tentar_escolher.
    vagas = elegibilidade.get("vagas_restantes") or 1
    ja_escolhidas = conn.execute(
        "SELECT COUNT(*) FROM ofertas_rota WHERE status = 'ESCOLHIDA' AND escolhido_por = ? AND data_alvo = ?",
        (agent_id, o["data_alvo"]),
    ).fetchone()[0]
    if ja_escolhidas >= vagas:
        raise OperacaoInvalida(
            f"Você já escolheu {ja_escolhidas} rota(s) pra esse dia -- é o máximo do seu cadastro. "
            f"Cancele uma escolha se quiser trocar.", 409)
    if marketplace is not None:
        from nucleo.marketplace_remoto import SemConexaoMarketplace
        try:
            status_http, corpo = marketplace.escolher(rascunho_id, agent_id)
        except SemConexaoMarketplace:
            raise OperacaoInvalida("Não consegui falar com o marketplace agora -- tente de novo em instantes.", 503)
        if status_http != 200:
            if status_http == 409 and "outro motorista" in (corpo.get("erro") or ""):
                # Espelha o que já aconteceu lá, pra lista do app parar de oferecer.
                conn.execute("UPDATE ofertas_rota SET status = 'ESCOLHIDA' WHERE rascunho_id = ? AND status = 'ABERTA'", (rascunho_id,))
                conn.commit()
            raise OperacaoInvalida(corpo.get("erro") or "Essa rota não está disponível pra você.",
                                   404 if status_http == 404 else 409)
        # Ganhou na página pública: espelha. sincronizado_em preenchido -- a
        # VPS já sabe, não há o que empurrar.
        conn.execute("""
            UPDATE ofertas_rota SET status = 'ESCOLHIDA', escolhido_por = ?, escolhido_em = ?, sincronizado_em = ?
            WHERE rascunho_id = ?
        """, (agent_id, corpo.get("escolhido_em") or banco.agora(), banco.agora(), rascunho_id))
        registrar_evento(conn, "OFERTA_ESCOLHIDA", banco.ORIGEM_APP, agent_id=agent_id,
                         dados={"rascunho_id": rascunho_id, "via": "marketplace"})
        conn.commit()
        return listar_ofertas(conn, agent_id)

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


def cancelar_escolha_oferta(conn: sqlite3.Connection, agent_id: int | None, rascunho_id: int, marketplace=None) -> dict:
    """Desiste da escolha pelo app. Só antes de a escolha ser aplicada no
    planejamento (aplicado_em). Com `marketplace`, cancela primeiro na
    página pública (dona da escolha) e espelha aqui."""
    if marketplace is not None:
        from nucleo.marketplace_remoto import SemConexaoMarketplace
        o = conn.execute("SELECT * FROM ofertas_rota WHERE rascunho_id = ?", (rascunho_id,)).fetchone()
        if not o or o["status"] != "ESCOLHIDA" or o["escolhido_por"] != agent_id or o["aplicado_em"] is not None:
            raise OperacaoInvalida("Essa escolha já foi aplicada no planejamento -- fale com a operação pra desfazer.", 409)
        try:
            status_http, corpo = marketplace.cancelar(rascunho_id, agent_id)
        except SemConexaoMarketplace:
            raise OperacaoInvalida("Não consegui falar com o marketplace agora -- tente de novo em instantes.", 503)
        if status_http != 200 and corpo.get("status") == "ESCOLHIDA" and corpo.get("escolhido_por") not in (None, agent_id):
            raise OperacaoInvalida(corpo.get("erro") or "Essa escolha não é mais sua.", 409)
        # 200, ou lá já não estava escolhida por ele (cancelada/retirada): espelha.
        conn.execute("""
            UPDATE ofertas_rota SET status = 'ABERTA', escolhido_por = NULL, escolhido_em = NULL, sincronizado_em = ?
            WHERE rascunho_id = ? AND status = 'ESCOLHIDA' AND escolhido_por = ? AND aplicado_em IS NULL
        """, (banco.agora(), rascunho_id, agent_id))
        registrar_evento(conn, "OFERTA_CANCELADA", banco.ORIGEM_APP, agent_id=agent_id,
                         dados={"rascunho_id": rascunho_id, "via": "marketplace"})
        conn.commit()
        return listar_ofertas(conn, agent_id)

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
