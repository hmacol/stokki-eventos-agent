# -*- coding: utf-8 -*-
"""
vigia/vigiar.py

Vigia de pedidos abertos (Hugo, 28/09: "pedidos atrasados, esquecidos ou
parados que não voltam pra rota -- resolver de uma vez por todas").

A cada rodada junta, SÓ DO BANCO (não chama Stokki nem Vuupt):
  - vigia_stokki_abertos  o que está aberto na Stokki (retrato do pipeline)
  - nucleo_pedidos        o estado de cada serviço na Vuupt (espelho de 15 min)
  - rascunhos_rota/parada rascunhos ativos de hoje em diante
  - nucleo_rotas          data da rota em que o pedido está
  - agendamentos_pedido   data pedida ao embarcador e sem resposta
  - fingerprints de insucesso + torre_excecoes_tratadas
e classifica cada pedido (vigia/regras.py): estado, desde quando, prazo.
Grava em vigia_pedidos (a Torre e a tela /vigia leem de lá) e registra
cada troca de estado em vigia_historico.

Rodar (timer stokki-vigia-pedidos, a cada 15 min):
    venv/bin/python -m vigia.vigiar
    venv/bin/python -m vigia.vigiar --resumo     # só imprime, não grava
"""
import argparse
import logging
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

from vigia import banco, regras  # noqa: E402

logger = logging.getLogger("vigia")

_PADRAO_BASE = re.compile(r"^([A-Z]{1,4}-?\d{2,})")
# Retrato mais velho que isso não serve pra dizer que um pedido "está aberto
# sem serviço" (o pipeline pode ter parado de rodar).
HORAS_RETRATO_VALIDO = 30


def base_do_codigo(codigo: str) -> str:
    m = _PADRAO_BASE.match(banco.normalizar(codigo))
    return m.group(1) if m else banco.normalizar(codigo)


def _partes(codigo: str) -> list[str]:
    return [banco.normalizar(p) for p in str(codigo or "").split(",") if p.strip()]


def _dt(texto) -> datetime | None:
    if not texto:
        return None
    t = str(texto).strip().replace("T", " ")[:19]
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        pass
    try:
        return datetime.strptime(t[:10], "%d/%m/%Y")  # agendamentos_pedido usa DD/MM/YYYY
    except ValueError:
        return None


def _d(texto) -> date | None:
    dt = _dt(texto)
    return dt.date() if dt else None


def _consultar(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    """Tabela que ainda não existe (banco novo, fluxo nunca rodou) = vazio.
    Qualquer outro erro (lock, coluna faltando) sobe: engolir faria, por
    exemplo, todo pedido aberto virar "sem serviço" numa rodada."""
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        logger.debug(f"consulta ignorada ({e}): {sql[:60]}")
        return []


# ── Coleta ──────────────────────────────────────────────────────────────────

def coletar_fatos(conn: sqlite3.Connection, hoje: date) -> list[dict]:
    """Um dict por pedido a vigiar, com os fatos que regras.classificar usa
    + os carimbos de tempo pra estimar o 'desde'."""
    abertos = {r["codigo"]: dict(r) for r in _consultar(conn, "SELECT * FROM vigia_stokki_abertos")}

    servicos = _consultar(conn, """
        SELECT codigo, vuupt_service_id, status, agendamento_inicio, vuupt_route_id,
               criado_em_provedor, atualizado_em_provedor, criado_em, remetente_nome,
               destinatario_nome, fluxo, excluido_em, reentrega_de_service_id
        FROM nucleo_pedidos
    """)
    duplicados = {r["service_id_original"]: r["cancelado_em"] for r in _consultar(
        conn, "SELECT service_id_original, cancelado_em FROM insucessos_duplicados")}
    # Serviço recriado a partir de outro (nosso fingerprint OU recriação à
    # mão na Vuupt, que só aparece no espelho via recreated_order_origin_id).
    recriados = {s["reentrega_de_service_id"] for s in servicos
                 if s["reentrega_de_service_id"] and s["status"] != "CANCELADO" and not s["excluido_em"]}

    # Bases (PS-X) que têm algum serviço vivo na Vuupt, de QUALQUER fluxo (a
    # retirada usa o mesmo código) -- quem está aberto na Stokki e não tem
    # nenhum é SEM_SERVICO.
    bases_com_servico: set[str] = set()
    ativos = []
    for s in servicos:
        vivo = s["status"] != "CANCELADO" and not s["excluido_em"]
        partes = _partes(s["codigo"])
        combinado_reentregue = (s["status"] == "INSUCESSO" and len(partes) > 1
                                and (s["vuupt_service_id"] in duplicados or s["vuupt_service_id"] in recriados))
        if vivo and not combinado_reentregue:
            # Combinado ("PS-1, PS-2") com insucesso já reentregue: a
            # reentrega leva só o 1º pedido -- os outros NÃO têm serviço vivo.
            for parte in partes:
                bases_com_servico.add(base_do_codigo(parte))
        elif vivo and combinado_reentregue:
            bases_com_servico.add(base_do_codigo(partes[0]))
        if (vivo and s["status"] in ("ABERTO", "EM_ROTA", "INSUCESSO")
                and (s["fluxo"] or "ENTREGA") == "ENTREGA"):
            ativos.append(s)

    rotas = {r["vuupt_route_id"]: _d(r["data_rota"]) for r in _consultar(
        conn, "SELECT vuupt_route_id, data_rota FROM nucleo_rotas WHERE vuupt_route_id IS NOT NULL")}

    # Rascunhos ativos: só o lote mais recente de cada data (é o que a tela
    # mostra), de hoje em diante.
    rascunhos: dict[int, dict] = {}
    lotes = {r["data_alvo"]: r["lote_id"] for r in _consultar(conn, """
        SELECT data_alvo, lote_id FROM rascunhos_rota r
        WHERE status != 'DESCARTADO' AND data_alvo >= ?
          AND criado_em = (SELECT MAX(criado_em) FROM rascunhos_rota r2
                           WHERE r2.data_alvo = r.data_alvo AND r2.status != 'DESCARTADO')
    """, (hoje.isoformat(),))}
    for r in _consultar(conn, """
        SELECT p.service_id, r.status, r.data_alvo, r.lote_id, r.criado_em, r.nome
        FROM rascunhos_parada p JOIN rascunhos_rota r ON r.id = p.rascunho_id
        WHERE r.status IN ('RASCUNHO', 'OFERTADA', 'ERRO_ENVIO') AND r.data_alvo >= ?
    """, (hoje.isoformat(),)):
        if lotes.get(r["data_alvo"]) == r["lote_id"]:
            rascunhos[r["service_id"]] = dict(r)

    agendamento_pendente = {}
    for r in _consultar(conn, """
        SELECT pedido, COALESCE(solicitado_em, enviado_em) AS desde FROM agendamentos_pedido
        WHERE status = 'PENDENTE'
    """):
        agendamento_pendente[banco.normalizar(r["pedido"])] = r["desde"]

    agendadas = {r["service_id"] for r in _consultar(
        conn, "SELECT service_id FROM duplicacoes_agendadas WHERE status IN ('PENDENTE', 'EXECUTADO')")}
    recusas = {r["service_id"]: r["respondido_em"] for r in _consultar(conn, """
        SELECT service_id, respondido_em FROM insucessos_aguardando_resposta
        WHERE status != 'PENDENTE' AND COALESCE(duplicado_apos_resposta, 1) = 0
    """)}
    tratadas = {r["id"].split(":", 1)[1].lstrip("#").upper() for r in _consultar(
        conn, "SELECT id FROM torre_excecoes_tratadas WHERE id LIKE 'insucesso:%'")}
    dedicados = {banco.normalizar(r["codigo_pedido"]) for r in _consultar(
        conn, "SELECT codigo_pedido FROM pedidos_dedicados WHERE removido_em IS NULL AND codigo_pedido IS NOT NULL")}
    area_nao_atendida = {r["service_id"]: r["notificado_em"] for r in _consultar(
        conn, "SELECT service_id, notificado_em FROM pedidos_area_notificada")}
    # Rota fraca adiada de propósito (roteirizacao/rotas_fracas.py): só
    # conta enquanto a data nova não chegou.
    segurados = {banco.normalizar(r["codigo"]): r["prazo_final"] for r in _consultar(
        conn, "SELECT codigo, prazo_final FROM pedidos_segurados WHERE data_nova > ?", (hoje.isoformat(),))}
    conclusoes = {r["service_id"]: (r["completed_at"], r["motivo_texto"]) for r in _consultar(conn, """
        SELECT service_id, MAX(completed_at) AS completed_at, motivo_texto FROM nucleo_paradas
        WHERE service_id IS NOT NULL AND completed_at IS NOT NULL GROUP BY service_id
    """)}

    fatos = []
    vistos_bases = set()
    for s in ativos:
        codigo = banco.normalizar(s["codigo"])
        base = base_do_codigo(_partes(codigo)[0]) if _partes(codigo) else codigo
        vistos_bases.add(base)
        sid = s["vuupt_service_id"]
        aberto = base in abertos
        # Insucesso de pedido que já saiu de "aberto" na Stokki (expedido,
        # cancelado) é história -- sem isso o espelho inteiro desde agosto
        # viraria alerta.
        if s["status"] == "INSUCESSO" and not aberto:
            continue
        rasc = rascunhos.get(sid) if sid else None
        concluido, motivo_insucesso = conclusoes.get(sid, (None, None))
        f = {
            "codigo": codigo, "service_id": sid, "status_nucleo": s["status"],
            "aberto_stokki": aberto,
            "agendamento": _d(s["agendamento_inicio"]),
            # Só pelo código exato: a -R1 não herda o pedido de data da original.
            "agendamento_pendente": codigo in agendamento_pendente,
            "rascunho_status": rasc["status"] if rasc else None,
            "rascunho_data": _d(rasc["data_alvo"]) if rasc else None,
            "rota_data": rotas.get(s["vuupt_route_id"]) if s["vuupt_route_id"] else None,
            "tem_reentrega": bool(sid and ((sid in duplicados and not duplicados[sid])
                                           or sid in agendadas or sid in recriados)),
            "recusado": bool(sid and (sid in recusas or duplicados.get(sid))),
            "tratado_na_torre": codigo in tratadas,
            "motivo_insucesso": motivo_insucesso,
            "motivo_pool": ("dedicado -- transporte cotado à parte, fora da rota compartilhada"
                            if codigo in dedicados or base in dedicados else
                            f"área não atendida (embarcador avisado em {str(area_nao_atendida[sid])[:10]})"
                            if sid in area_nao_atendida else
                            f"segurado para consolidar (prazo {_d(segurados[base]):%d/%m})"
                            if base in segurados else None),
            "prazo_segurado": _d(segurados[base]) if base in segurados else None,
            # carimbos pro 'desde'
            "_criado": _dt(s["criado_em_provedor"]) or _dt(s["criado_em"]),
            "_atualizado": _dt(s["atualizado_em_provedor"]),
            "_concluido": _dt(concluido),
            "_rascunho_criado": _dt(rasc["criado_em"]) if rasc else None,
            "_agendamento_desde": _dt(agendamento_pendente.get(codigo)),
            "_recusado_em": _dt(recusas.get(sid)) if sid else None,
            "_detalhe": " | ".join(x for x in (
                abertos.get(base, {}).get("embarcador") or s["remetente_nome"],
                s["destinatario_nome"],
                (rasc or {}).get("nome"),
            ) if x),
        }
        fatos.append(f)

    for base, a in abertos.items():
        if base in bases_com_servico or base in vistos_bases:
            continue
        fatos.append({
            "codigo": base, "service_id": None, "status_nucleo": None, "aberto_stokki": True,
            "acao_pipeline": a["ultima_acao"], "obs_pipeline": a["ultima_obs"],
            "_primeira_vez": _dt(a["primeira_vez_em"]),
            "_detalhe": " | ".join(x for x in (a["embarcador"], a["status_stokki"]) if x),
        })
    return fatos


def _desde_estimado(estado: str, f: dict, agora: datetime) -> datetime:
    """Quando o pedido entrou no estado, na primeira vez que o vigia o vê
    nele (depois vale o carimbo gravado em vigia_pedidos)."""
    candidatos = {
        regras.SEM_SERVICO: f.get("_primeira_vez"),
        regras.NO_POOL: f.get("_criado"),
        regras.AGENDADO: f.get("_criado"),
        regras.AGUARDANDO_CLIENTE: f.get("_agendamento_desde") or f.get("_criado"),
        regras.EM_RASCUNHO: f.get("_rascunho_criado"),
        regras.RASCUNHO_COM_ERRO: f.get("_rascunho_criado"),
        regras.INSUCESSO: f.get("_concluido") or f.get("_atualizado"),
        regras.RECUSADO: f.get("_recusado_em") or f.get("_atualizado"),
        regras.ROTA_PASSADA: (datetime.combine(f["rota_data"] + timedelta(days=1), datetime.min.time())
                              if f.get("rota_data") else None),
        regras.EM_ROTA: f.get("_atualizado"),
    }
    return min(candidatos.get(estado) or agora, agora)


# ── Rodada ──────────────────────────────────────────────────────────────────

def rodar(conn: sqlite3.Connection, agora: datetime | None = None, gravar: bool = True) -> dict:
    agora = (agora or datetime.now()).replace(microsecond=0)
    hoje = agora.date()
    agora_txt = agora.strftime(banco.FMT)

    ultima = banco.ultima_listagem_completa(conn)
    retrato_ok = bool(ultima and agora - _dt(ultima) < timedelta(hours=HORAS_RETRATO_VALIDO))
    if not retrato_ok:
        logger.warning(f"Retrato da Stokki velho ou ausente (última listagem completa: {ultima}) -- "
                       f"'sem serviço' e insucessos ficam de fora desta rodada.")

    anteriores = {r["codigo"]: dict(r) for r in conn.execute("SELECT * FROM vigia_pedidos")}
    novos: dict[str, dict] = {}
    for f in coletar_fatos(conn, hoje):
        if not retrato_ok and (f["status_nucleo"] is None or f["status_nucleo"] == "INSUCESSO"):
            continue
        res = regras.classificar(f, hoje)
        if not res:
            continue
        estado, motivo = res
        ant = anteriores.get(f["codigo"])
        if ant and ant["estado"] == estado:
            desde = _dt(ant["desde"]) or agora
        else:
            desde = _desde_estimado(estado, f, agora)
        vence = regras.prazo(estado, desde, data_rascunho=f.get("rascunho_data"),
                             prazo_segurado=f.get("prazo_segurado"))
        novos[f["codigo"]] = {
            "codigo": f["codigo"], "estado": estado, "motivo": (motivo or "")[:300],
            "desde": desde.strftime(banco.FMT),
            "vence_em": vence.strftime(banco.FMT) if vence else None,
            "vencido": 1 if regras.vencido(vence, agora) else 0,
            "service_id": f.get("service_id"), "detalhe": (f.get("_detalhe") or "")[:300],
            "visto_em": agora_txt,
        }

    if not retrato_ok:
        # Cego pra Stokki: mantém o que já se sabia em vez de "resolver"
        # (apagar) justamente quando não dá pra ver -- a Torre ganha o
        # aviso de retrato velho (consulta.excecoes_torre).
        for codigo, ant in anteriores.items():
            if codigo not in novos and ant["estado"] in (regras.SEM_SERVICO, regras.INSUCESSO, regras.RECUSADO):
                vence = _dt(ant["vence_em"])
                novos[codigo] = {**ant, "vencido": 1 if regras.vencido(vence, agora) else 0,
                                 "visto_em": agora_txt}

    resumo = {"total": len(novos), "vencidos": sum(n["vencido"] for n in novos.values()),
              "por_estado": {}, "retrato_ok": retrato_ok, "ultima_listagem": ultima}
    for n in novos.values():
        e = resumo["por_estado"].setdefault(n["estado"], {"total": 0, "vencidos": 0})
        e["total"] += 1
        e["vencidos"] += n["vencido"]
    if not gravar:
        return resumo

    for codigo, ant in anteriores.items():
        novo = novos.get(codigo)
        if novo is None or novo["estado"] != ant["estado"]:
            conn.execute("INSERT INTO vigia_historico (codigo, estado, motivo, desde, ate) VALUES (?, ?, ?, ?, ?)",
                         (codigo, ant["estado"], ant["motivo"], ant["desde"], agora_txt))
        if novo is None:
            conn.execute("DELETE FROM vigia_pedidos WHERE codigo = ?", (codigo,))
    for n in novos.values():
        conn.execute("""
            INSERT INTO vigia_pedidos (codigo, estado, motivo, desde, vence_em, vencido, service_id, detalhe, visto_em)
            VALUES (:codigo, :estado, :motivo, :desde, :vence_em, :vencido, :service_id, :detalhe, :visto_em)
            ON CONFLICT(codigo) DO UPDATE SET
                estado = excluded.estado, motivo = excluded.motivo, desde = excluded.desde,
                vence_em = excluded.vence_em, vencido = excluded.vencido,
                service_id = excluded.service_id, detalhe = excluded.detalhe, visto_em = excluded.visto_em
        """, n)
    conn.commit()
    return resumo


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Vigia de pedidos abertos (estado, idade e prazo).")
    parser.add_argument("--resumo", action="store_true", help="Só calcula e imprime, não grava.")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    conn = banco.conectar()
    try:
        resumo = rodar(conn, gravar=not args.resumo)
    finally:
        conn.close()
    logger.info(f"Vigia: {resumo['total']} pedido(s) vigiado(s), {resumo['vencidos']} com prazo vencido.")
    for estado in regras.ORDEM:
        if estado in resumo["por_estado"]:
            e = resumo["por_estado"][estado]
            logger.info(f"  {regras.ROTULOS[estado]}: {e['total']} ({e['vencidos']} vencido(s))")


if __name__ == "__main__":
    main()
