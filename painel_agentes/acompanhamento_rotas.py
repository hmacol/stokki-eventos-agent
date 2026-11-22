# -*- coding: utf-8 -*-
"""
acompanhamento_rotas.py

Dados da tela /acompanhamento (pedido do Hugo, 21/11): a logistica acompanha
cada rota do dia com mais detalhe que a Torre -- tempo por parada real x
planejado, ocorrencias, despesas e posicao do motorista. So le o nucleo
(nucleo_rotas / nucleo_paradas / nucleo_eventos / nucleo_pedagios); nao
escreve nada e nao chama Stokki nem Vuupt.

Por que "tempo por parada" e nao deslocamento/espera dos cliques: 70% das
paradas da Vuupt tem deslocamento < 1 min, porque o motorista aperta sai /
cheguei / entreguei juntos na porta (medido na VPS em 21/11). Entao:

  real      = conclusao desta parada - conclusao da anterior (ordem de
              CONCLUSAO, nao a `ordem` planejada); a primeira conta de
              nucleo_rotas.iniciada_em.
  planejado = trajeto + atendimento (+ espera) previstos pela Vuupt
              (dados_json.route_service.prevision_initial_*). O atendimento
              previsto e fixo em 30 min, entao por parada o motorista quase
              sempre aparece adiantado.
  atraso da rota = acumulado LIQUIDO: max(0, soma real - soma planejado).
              Somar so os atrasos deixava 47 de 69 rotas vermelhas, porque
              parada baixada em lote joga o tempo todo na seguinte.

Deslocamento e espera separados so aparecem quando os cliques parecem reais
(tempo_deslocamento_s >= 60).
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from nucleo import banco  # noqa: E402

# Cortes do semaforo (sugestao aprovada no desenho; atraso acumulado da rota).
VERDE_ATE_S = 600
AMARELO_ATE_S = 1800
# Abaixo disso o clique nao e uma medida: parada baixada junto com a anterior
# (ciclo) ou sai/cheguei apertados na porta (deslocamento).
CLIQUE_MINIMO_S = 60
# Ultimo ponto de GPS mais novo que isso = "ao vivo".
GPS_AO_VIVO_S = 600

_FORMATO = "%Y-%m-%d %H:%M:%S"
_REENTREGA = re.compile(r"-R\d*$", re.IGNORECASE)


def semaforo(atraso_s) -> str:
    if atraso_s is None:
        return "cinza"
    if atraso_s <= VERDE_ATE_S:
        return "verde"
    if atraso_s <= AMARELO_ATE_S:
        return "amarelo"
    return "vermelho"


def _hora(texto):
    try:
        return datetime.strptime(str(texto)[:19].replace("T", " "), _FORMATO)
    except (TypeError, ValueError):
        return None


def _previsto(parada: dict) -> dict:
    try:
        dados = json.loads(parada.get("dados_json") or "{}")
    except ValueError:
        return {}
    rs = dados.get("route_service") if isinstance(dados, dict) else None
    return rs if isinstance(rs, dict) else {}


def _plan_s(parada: dict):
    rs = _previsto(parada)
    trajeto, atendimento = rs.get("prevision_initial_trip_time"), rs.get("prevision_initial_duration_time")
    if trajeto is None or atendimento is None:
        return None
    try:
        return int(trajeto) + int(atendimento) + int(rs.get("prevision_initial_waiting_time") or 0)
    except (TypeError, ValueError):   # valor estranho numa parada nao derruba a lista do dia
        return None


def ciclos_paradas(paradas: list, iniciada_em) -> dict:
    """{parada_id: {real_s, plan_s, dif_s, junto, cliques_ok}} das paradas concluidas."""
    concluidas = sorted((p for p in paradas if _hora(p.get("completed_at"))),
                        key=lambda p: (_hora(p["completed_at"]), p.get("ordem") or 0))
    anterior, saida = _hora(iniciada_em), {}
    for i, p in enumerate(concluidas):
        fim = _hora(p["completed_at"])
        real = int((fim - anterior).total_seconds()) if anterior else None
        if real is not None and real < 0:   # iniciada_em gravada depois da 1a conclusao: nao e medida
            real = None
        plan = _plan_s(p)
        desloc = p.get("tempo_deslocamento_s")
        saida[p["id"]] = {
            "real_s": real,
            "plan_s": plan,
            "dif_s": real - plan if real is not None and plan is not None else None,
            "junto": i > 0 and real is not None and real < CLIQUE_MINIMO_S,
            "cliques_ok": desloc is not None and desloc >= CLIQUE_MINIMO_S,
        }
        anterior = fim
    return saida


def _atraso_rota(ciclos: dict):
    difs = [c["dif_s"] for c in ciclos.values() if c["dif_s"] is not None]
    return max(0, sum(difs)) if difs else None


def _paradas(conn, rota_id: int) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM nucleo_paradas WHERE rota_id = ? AND situacao != ? ORDER BY ordem, id",
        (rota_id, banco.PARADA_CANCELADA))]


def listar_rotas(conn, data_rota: str) -> list:
    rotas = conn.execute(
        "SELECT * FROM nucleo_rotas WHERE data_rota = ? AND status != ? ORDER BY motorista_nome, nome, id",
        (data_rota, banco.ROTA_CANCELADA)).fetchall()
    gps = {r["rota_id"]: r["ultimo"] for r in conn.execute(
        # pelo indice da rota: o de tipo varreria todo ponto de GPS ja gravado a cada atualizacao
        "SELECT e.rota_id, MAX(e.ocorrido_em) AS ultimo FROM nucleo_eventos e INDEXED BY idx_nucleo_eventos_rota"
        " WHERE e.rota_id IN (SELECT id FROM nucleo_rotas WHERE data_rota = ?) AND e.tipo = 'GPS'"
        " GROUP BY e.rota_id", (data_rota,))}
    saida = []
    for r in rotas:
        paradas = _paradas(conn, r["id"])
        ciclos = ciclos_paradas(paradas, r["iniciada_em"])
        atraso = _atraso_rota(ciclos)
        saida.append({
            "rota_id": r["id"], "nome": r["nome"], "motorista": r["motorista_nome"],
            "tipo_veiculo": r["tipo_veiculo"], "status": r["status"],
            "total_paradas": len(paradas), "concluidas": len(ciclos),
            "atraso_s": atraso, "semaforo": semaforo(atraso),
            "tem_gps": r["id"] in gps, "ultimo_gps_em": gps.get(r["id"]),
        })
    return saida


def _posicao(conn, rota_id: int, linhas: list, agora: datetime) -> dict:
    """Onde o carro esta: ultimo ponto de GPS do app; sem GPS, a ultima parada
    concluida que tem coordenada (motorista so na Vuupt / iPhone sem app)."""
    ponto = conn.execute(
        "SELECT latitude, longitude, ocorrido_em FROM nucleo_eventos INDEXED BY idx_nucleo_eventos_rota"
        " WHERE rota_id = ? AND tipo = 'GPS'"
        " AND latitude IS NOT NULL AND longitude IS NOT NULL ORDER BY ocorrido_em DESC, id DESC LIMIT 1",
        (rota_id,)).fetchone()
    concluidas = [x for x in linhas if x["concluida_em"] and x["lat"] is not None and x["lng"] is not None]
    ultima = max(concluidas, key=lambda x: x["concluida_em"]) if concluidas else None
    # app parou de mandar e as paradas seguiram sendo baixadas: vale o que for mais novo
    if ponto and ultima and str(ponto["ocorrido_em"])[:19].replace("T", " ") < ultima["concluida_em"][:19]:
        ponto = None
    if ponto:
        quando = _hora(ponto["ocorrido_em"])
        ha = max(0, int((agora - quando).total_seconds())) if quando else None
        return {"lat": ponto["latitude"], "lng": ponto["longitude"], "fonte": "GPS", "ha_s": ha,
                "ao_vivo": ha is not None and ha <= GPS_AO_VIVO_S}
    if ultima:
        quando = _hora(ultima["concluida_em"])
        return {"lat": ultima["lat"], "lng": ultima["lng"], "fonte": "ULTIMA_PARADA",
                "ha_s": max(0, int((agora - quando).total_seconds())) if quando else None, "ao_vivo": False}
    return {"lat": None, "lng": None, "fonte": None, "ha_s": None, "ao_vivo": False}


def detalhe_rota(conn, rota_id: int, agora: datetime | None = None, com_despesas: bool = False):
    """Tudo da rota escolhida. `com_despesas` so pra quem abre /financeiro/pedagios:
    sem ele as chaves de despesa nem existem na resposta."""
    r = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone()
    if r is None:
        return None
    agora = agora or datetime.now()
    paradas = _paradas(conn, rota_id)
    ciclos = ciclos_paradas(paradas, r["iniciada_em"])
    ja_canceladas = {e["parada_id"] for e in conn.execute(
        "SELECT parada_id FROM nucleo_eventos INDEXED BY idx_nucleo_eventos_rota"
        " WHERE rota_id = ? AND tipo = 'FINALIZACAO_CANCELADA'", (rota_id,))}
    vazio = {"real_s": None, "plan_s": None, "dif_s": None, "junto": False, "cliques_ok": False}
    linhas = []
    for p in paradas:
        c = ciclos.get(p["id"], vazio)
        linhas.append({
            "parada_id": p["id"], "finalizacao_cancelada": p["id"] in ja_canceladas,
            "ordem": p["ordem"], "codigo": p["codigo"], "destinatario": p["destinatario_nome"],
            "situacao": p["situacao"], "concluida_em": p["completed_at"] if p["id"] in ciclos else None,
            "real_s": c["real_s"], "plan_s": c["plan_s"] if p["id"] in ciclos else _plan_s(p),
            "dif_s": c["dif_s"], "junto": c["junto"], "cliques_ok": c["cliques_ok"],
            "desloc_s": p["tempo_deslocamento_s"] if c["cliques_ok"] else None,
            "no_local_s": p["tempo_no_local_s"] if c["cliques_ok"] else None,
            "ocorrencia": (p["motivo_texto"] or "").strip() or None,
            "lat": p["latitude"], "lng": p["longitude"],
            "reentrega": bool(_REENTREGA.search(p["codigo"] or "")),
        })
    # concluidas na ordem em que foram concluidas; depois as pendentes, na ordem planejada
    linhas.sort(key=lambda x: (0, x["concluida_em"], x["ordem"]) if x["concluida_em"] else (1, "", x["ordem"]))
    atraso = _atraso_rota(ciclos)
    saida = {
        "rota": {"rota_id": r["id"], "nome": r["nome"], "motorista": r["motorista_nome"],
                 "tipo_veiculo": r["tipo_veiculo"], "status": r["status"], "iniciada_em": r["iniciada_em"],
                 "total_paradas": len(paradas), "concluidas": len(ciclos),
                 "atraso_s": atraso, "semaforo": semaforo(atraso), "atualizado_em": r["atualizado_em"]},
        "paradas": linhas,
        "posicao": _posicao(conn, rota_id, linhas, agora),
        "ocorrencias": [{"codigo": x["codigo"], "destinatario": x["destinatario"], "motivo": x["ocorrencia"]}
                        for x in linhas if x["ocorrencia"]],
    }
    if com_despesas:
        from nucleo import operacao
        despesas = operacao.listar_pedagios(conn, rota_id)
        saida["despesas"] = despesas
        saida["total_despesas"] = round(sum(
            (d["valor_aprovado"] if d["valor_aprovado"] is not None else d["valor_informado"]) or 0
            for d in despesas if d["status"] not in (banco.PEDAGIO_REJEITADO, banco.PEDAGIO_CANCELADO)), 2)
    return saida
