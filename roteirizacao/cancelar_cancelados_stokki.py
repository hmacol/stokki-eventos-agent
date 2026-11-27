# -*- coding: utf-8 -*-
"""
roteirizacao/cancelar_cancelados_stokki.py

Pedido cancelado direto na Stokki DEPOIS de o servico ja existir na Vuupt
ficava vivo: o pipeline so le os abertos da Stokki e o batimento (07:25)
chega com o pedido ja na rota do dia. Caso PS-44486 (17/11): criado na
Vuupt 11:17, cancelado na Stokki 12:03, ficou no pool a tarde toda e saiu
na rota de 18/11 como "nao coletado".

A cada 15 min (07h-22h, infra/stokki-cancelar-cancelados-stokki.timer):
cruza os cancelados do coletor do galpao (galpao_pedidos situacao =
'Cancelado'; o coletor roda das 03:50 as 18:30) com os servicos vivos do
nucleo (ABERTO/EM_ROTA, inclusive -R/-C do mesmo pedido) e le o status
AGORA na Vuupt:
  - not_assigned, fora de rota enviada/ofertada -> cancela pelo mesmo
    caminho do botao do Planejamento (cancelar_servico_completo: PUT
    /cancel, tira do rascunho, ressincroniza o nucleo);
  - em rota (qualquer outro status vivo, ou rascunho ENVIADO/OFERTADA)
    -> nao mexe; e-mail interno uma vez por servico;
  - done/canceled/sumiu -> nada (o nucleo estava atras).
Decisao do Hugo (18/11): sozinho, so cancela o que esta no pool.

  py -3.11 -m roteirizacao.cancelar_cancelados_stokki [--modo-teste]
--modo-teste so lista o que faria: nao cancela, nao grava, nao manda e-mail.
"""
import argparse
import html
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from nucleo.normalizacao import normalizar_codigo  # noqa: E402
from roteirizacao import cancelar_servico as cs  # noqa: E402

logger = logging.getLogger("cancelar_cancelados_stokki")

DB_PATH = _RAIZ / "dados" / "dados.db"
CONFIG_PATH = _RAIZ / "config.yaml"
FMT = "%Y-%m-%d %H:%M:%S"
_RE_BASE = re.compile(r"^(PS-\d+)")
# rascunho nesses estados ja tem (ou pode ter) motorista: e rota, nao pool
_RASCUNHO_COM_MOTORISTA = (cs.rascunhos_rota.STATUS_ENVIADO, cs.rascunhos_rota.STATUS_OFERTADA)

CANCELADO = "CANCELADO"
EM_ROTA = "EM_ROTA"
FALHA = "FALHA"


def garantir_tabela(conn: sqlite3.Connection) -> None:
    """Uma linha por servico ja tratado: CANCELADO (auditoria), EM_ROTA ou
    FALHA (so pra nao repetir o e-mail a cada 15 min)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cancelados_stokki_vuupt (
            vuupt_service_id INTEGER PRIMARY KEY,
            codigo           TEXT NOT NULL,
            acao             TEXT NOT NULL,     -- CANCELADO | EM_ROTA | FALHA
            status_vuupt     TEXT,
            detalhe          TEXT,
            em               TEXT NOT NULL
        )""")
    conn.commit()


def base_do_codigo(codigo) -> str | None:
    """'#PS-1-R2' -> 'PS-1'. Servico com mais de um pedido -> None (nao existe
    mais; se aparecer, nao cancela por um pedido so)."""
    c = normalizar_codigo(codigo)
    if not c or "," in c:
        return None
    m = _RE_BASE.match(c)
    return m.group(1) if m else None


def candidatos(conn: sqlite3.Connection, incluir_sem_servico: bool = False) -> list[dict]:
    """Servicos vivos no nucleo cujo pedido esta Cancelado no galpao. Com `incluir_sem_servico`
    (planejamento.fonte_pool = stokki) entra tambem o pedido ABERTO sem servico na Vuupt, com o id
    sintetico do pool (-id_pool) e "sem_servico": True."""
    cancelados = {}
    for r in conn.execute("SELECT codigo, embarcador FROM galpao_pedidos WHERE situacao = 'Cancelado'"):
        b = base_do_codigo(r[0])
        if b:
            cancelados[b] = r[1] or ""
    if not cancelados:
        return []
    saida = []
    for r in conn.execute("""
            SELECT codigo, vuupt_service_id FROM nucleo_pedidos
            WHERE status IN ('ABERTO', 'EM_ROTA') AND excluido_em IS NULL
              AND vuupt_service_id IS NOT NULL AND codigo GLOB 'PS-*'"""):
        b = base_do_codigo(r[0])
        if b in cancelados:
            saida.append({"codigo": r[0], "service_id": int(r[1]), "embarcador": cancelados[b]})
    if incluir_sem_servico:
        for r in conn.execute("""
                SELECT codigo, id_pool FROM nucleo_pedidos
                WHERE status = 'ABERTO' AND excluido_em IS NULL AND vuupt_service_id IS NULL
                  AND id_pool IS NOT NULL AND codigo GLOB 'PS-*'"""):
            b = base_do_codigo(r[0])
            if b in cancelados:
                saida.append({"codigo": r[0], "service_id": -int(r[1]), "embarcador": cancelados[b],
                              "sem_servico": True})
    return saida


def _ja_registrado(conn, service_id: int) -> str | None:
    row = conn.execute("SELECT acao FROM cancelados_stokki_vuupt WHERE vuupt_service_id = ?",
                       (service_id,)).fetchone()
    return row[0] if row else None


def _registrar(conn, c: dict, acao: str, status: str, detalhe: str, agora_txt: str) -> None:
    conn.execute("""
        INSERT INTO cancelados_stokki_vuupt (vuupt_service_id, codigo, acao, status_vuupt, detalhe, em)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(vuupt_service_id) DO UPDATE SET
            acao = excluded.acao, status_vuupt = excluded.status_vuupt,
            detalhe = excluded.detalhe, em = excluded.em
    """, (c["service_id"], c["codigo"], acao, status, detalhe[:300], agora_txt))
    conn.commit()


def _cancelar_sem_servico(conn, c: dict, rascunho_id, status_rascunho, res: dict, item: dict,
                          agora_txt: str) -> None:
    """Pedido sem servico na Vuupt: CANCELADO no nucleo e fora do rascunho nao enviado. Nunca chama
    a Vuupt (o id e sintetico)."""
    from nucleo import banco as nucleo_banco
    from nucleo.pedidos import cancelar_sem_servico
    mudou = cancelar_sem_servico(c["codigo"], "cancelado na Stokki", nucleo_banco.ORIGEM_PIPELINE, conn=conn)
    if rascunho_id and status_rascunho != cs.rascunhos_rota.STATUS_ENVIADO:
        cs.rascunhos_rota.remover_parada(rascunho_id, c["service_id"])
    item["detalhe"] = ("cancelado no nucleo (sem servico na Vuupt)" if mudou
                       else "ja estava fechado no nucleo (sem servico na Vuupt)")
    logger.info(f"{c['codigo']}: {item['detalhe']} (cancelado na Stokki)")
    _registrar(conn, c, CANCELADO, "sem_servico", item["detalhe"], agora_txt)
    res["cancelados"].append(item)


def processar(conn: sqlite3.Connection, vuupt, token: str, modo_teste: bool = False,
              agora: datetime | None = None, incluir_sem_servico: bool = False) -> dict:
    """Devolve {"cancelados", "em_rota", "falhas"} so com o que e NOVO nesta
    rodada (em_rota/falha ja avisados nao voltam), mais "vistos".
    `incluir_sem_servico` (fonte stokki): pedido sem servico na Vuupt fecha LOCAL no nucleo."""
    agora_txt = (agora or datetime.now()).strftime(FMT)
    res = {"cancelados": [], "em_rota": [], "falhas": [], "vistos": 0}
    for c in candidatos(conn, incluir_sem_servico):
        res["vistos"] += 1
        sid = c["service_id"]
        if c.get("sem_servico"):
            status = "sem_servico"   # nada a ler na Vuupt
        else:
            atual = vuupt.buscar_servico_por_id(sid)
            if not atual:
                logger.warning(f"{c['codigo']}: servico {sid} nao respondeu na Vuupt; tenta na proxima rodada")
                continue
            status = atual.get("status") or ""
            if status in ("done", "canceled"):
                continue  # nucleo atrasado: nada a fazer
        rascunho_id, status_rascunho = cs.rascunho_do_servico(sid)
        no_pool = (status in (cs.VuuptClient.STATUS_ATUALIZAVEL, "sem_servico")
                   and status_rascunho not in _RASCUNHO_COM_MOTORISTA)
        item = {**c, "status": status, "rascunho": rascunho_id}

        if not no_pool:
            onde = f"rascunho {rascunho_id} {status_rascunho}" if status_rascunho in _RASCUNHO_COM_MOTORISTA else ""
            item["detalhe"] = f"status {status}" + (f", {onde}" if onde else "")
            logger.info(f"{c['codigo']}: cancelado na Stokki mas em rota na Vuupt ({item['detalhe']}): nao mexe")
            if modo_teste:
                res["em_rota"].append(item)
            elif _ja_registrado(conn, sid) != EM_ROTA:  # avisa uma vez por servico
                _registrar(conn, c, EM_ROTA, status, item["detalhe"], agora_txt)
                res["em_rota"].append(item)
            continue

        if modo_teste:
            logger.info(f"[MODO TESTE] {c['codigo']}: cancelaria o servico {sid} (no pool)")
            res["cancelados"].append(item)
            continue
        if c.get("sem_servico"):
            _cancelar_sem_servico(conn, c, rascunho_id, status_rascunho, res, item, agora_txt)
            continue
        r = cs.cancelar_servico_completo(token, sid, vuupt=vuupt)
        if r.get("ok"):
            item["detalhe"] = "já estava cancelado" if r.get("ja_estava") else "cancelado no pool"
            logger.info(f"{c['codigo']}: servico {sid} cancelado na Vuupt (cancelado na Stokki)")
            _registrar(conn, c, CANCELADO, status, item["detalhe"], agora_txt)
            res["cancelados"].append(item)
        else:
            item["detalhe"] = r.get("erro") or "falha sem mensagem"
            logger.warning(f"{c['codigo']}: falha ao cancelar o servico {sid}: {item['detalhe']}")
            if _ja_registrado(conn, sid) != FALHA:
                res["falhas"].append(item)
            _registrar(conn, c, FALHA, status, item["detalhe"], agora_txt)
    return res


def montar_email(res: dict) -> tuple[str, str]:
    e = html.escape
    n_c, n_r, n_f = len(res["cancelados"]), len(res["em_rota"]), len(res["falhas"])
    partes_assunto = []
    if n_c:
        partes_assunto.append(f"{n_c} cancelado(s) na Vuupt")
    if n_r:
        partes_assunto.append(f"{n_r} em rota")
    if n_f:
        partes_assunto.append(f"{n_f} falha(s)")
    assunto = "[Freshlog] Cancelados na Stokki: " + ", ".join(partes_assunto)

    def tabela(titulo: str, itens: list[dict], nota: str) -> str:
        linhas = "".join(
            f"<tr><td style='padding:3px 10px 3px 0'>{e(i['codigo'])}</td>"
            f"<td style='padding:3px 10px 3px 0'>{e(i['embarcador'])}</td>"
            f"<td style='padding:3px 10px 3px 0'>{i['service_id']}</td>"
            f"<td style='padding:3px 0;color:#6B7280'>{e(i.get('detalhe') or '')}</td></tr>" for i in itens)
        return (f"<h3 style='margin:16px 0 6px'>{e(titulo)}</h3><p style='margin:0 0 6px'>{e(nota)}</p>"
                "<table style='border-collapse:collapse;font-size:13px'><tr><th align='left'>Pedido</th>"
                "<th align='left'>Embarcador</th><th align='left'>Serviço</th><th align='left'>Detalhe</th></tr>"
                f"{linhas}</table>")

    corpo = ["<p style='margin:0 0 8px'>Pedidos cancelados na Stokki que ainda tinham serviço vivo na Vuupt.</p>"]
    if res["em_rota"]:
        corpo.append(tabela("Em rota: tirar à mão", res["em_rota"],
                            "Já estão em rota com motorista. A rotina não mexe: avisar o motorista e cancelar "
                            "pelo Planejamento se a mercadoria não vai sair."))
    if res["falhas"]:
        corpo.append(tabela("Falha ao cancelar", res["falhas"], "A rotina tenta de novo a cada 15 min."))
    if res["cancelados"]:
        corpo.append(tabela("Cancelados na Vuupt", res["cancelados"], "Estavam no pool, sem rota."))
    return assunto, "".join(corpo)


def avisar(config: dict, res: dict) -> bool:
    """E-mail interno so quando ha novidade. Nunca levanta (o cancelamento ja
    aconteceu; falha de e-mail so loga)."""
    if not (res["cancelados"] or res["em_rota"] or res["falhas"]):
        return False
    try:
        from email_utils import COR_DESTAQUE, COR_ERRO, envelope_html, enviar_email
        destino = (config.get("notificacao_execucao") or {}).get("destinatario") or "hugo@freshlogbr.com"
        assunto, corpo = montar_email(res)
        cor = COR_ERRO if (res["em_rota"] or res["falhas"]) else COR_DESTAQUE
        ok = enviar_email([destino], assunto, envelope_html(corpo, cor_acento=cor), config.get("email", {}))
        logger.info(f"E-mail {'enviado' if ok else 'NAO enviado'} para {destino}: {assunto}")
        return bool(ok)
    except Exception as ex:  # noqa: BLE001
        logger.exception(f"Falha ao mandar o e-mail: {ex}")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Cancela na Vuupt (so no pool) o que foi cancelado na Stokki.")
    ap.add_argument("--modo-teste", action="store_true", help="so lista: nao cancela, nao grava, nao manda e-mail")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    token = (config.get("vuupt_api") or {}).get("token")
    if not token:
        logger.error("Token da Vuupt ausente no config.")
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        if not args.modo_teste:  # modo teste nao escreve nada no banco
            garantir_tabela(conn)
        from nucleo import pool
        res = processar(conn, cs.VuuptClient(token), token, modo_teste=args.modo_teste,
                        incluir_sem_servico=pool.fonte_pool(config) == pool.FONTE_STOKKI)
    finally:
        conn.close()
    logger.info(f"{'[MODO TESTE] ' if args.modo_teste else ''}vistos={res['vistos']} "
                f"cancelados={len(res['cancelados'])} em_rota_novos={len(res['em_rota'])} "
                f"falhas_novas={len(res['falhas'])}")
    if not args.modo_teste:
        avisar(config, res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
