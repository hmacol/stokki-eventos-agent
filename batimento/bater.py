# -*- coding: utf-8 -*-
"""
batimento/bater.py

Job diario do batimento (DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md): faz a
mesma coleta do medir.py (Stokki Sent/Canceled/abertos desde o corte +
Vuupt done + banco), classifica cada pedido e grava em batimento_pedidos /
batimento_rodadas (batimento/banco.py). Nao escreve na Stokki nem na Vuupt.

Equacao que nao fecha = sai com codigo 1, e o OnFailure do systemd manda o
alerta de falha (stokki-alerta-falha@).

Execute (VPS, como www-data):
  sudo -u www-data venv/bin/python -m batimento.bater
  sudo -u www-data venv/bin/python -m batimento.bater --resumo   # so imprime, nao grava
"""
import argparse
import html
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from batimento import banco, consulta, medir  # noqa: E402

logger = logging.getLogger("batimento.bater")

DONO_TRAVA = "batimento"
LINK_ABA = "https://app.freshhub.com.br/painel/vigia?aba=fechamento"


def montar_email(novas: list[dict], rodada: dict, por_motivo: list[dict]) -> tuple[str, str]:
    """(assunto, html sem envelope). Novidade = divergencia nova nesta
    rodada ou conta que nao fecha (decisao do Hugo, 05/10)."""
    if not rodada["fecha"]:
        assunto = f"[Freshlog] Batimento NÃO FECHA: {rodada['lancados']} lançados"
    else:
        assunto = f"[Freshlog] Batimento: {len(novas)} divergência(s) nova(s)"
    e = html.escape
    selo = "fecha" if rodada["fecha"] else "<b style='color:#EF4444'>NÃO FECHA</b>"
    partes = [
        f"<h2 style='margin:0 0 8px'>Batimento de {e(rodada['rodada_em'][:16])}</h2>",
        f"<p style='margin:0 0 12px'><b>{rodada['lancados']} lançados</b> = {rodada['destino']} destino + "
        f"{rodada['em_andamento']} em andamento + {rodada['divergencia']} divergência — {selo}.</p>",
    ]
    if novas:
        partes.append("<h3 style='margin:16px 0 6px'>Divergências novas</h3>"
                      "<table style='border-collapse:collapse;font-size:13px'>"
                      "<tr><th align='left'>Pedido</th><th align='left'>Embarcador</th>"
                      "<th align='left'>Motivo</th><th align='left'>Evidências</th></tr>")
        for n in novas:
            motivo = e(n["motivo_txt"])
            if n.get("real"):
                motivo = f"<b>{motivo}</b>"
            partes.append(
                f"<tr><td style='padding:3px 10px 3px 0'>{e(n['codigo'])}</td>"
                f"<td style='padding:3px 10px 3px 0'>{e(n.get('embarcador') or '')}</td>"
                f"<td style='padding:3px 10px 3px 0'>{motivo}</td>"
                f"<td style='padding:3px 0;color:#6B7280'>{e(' · '.join(n.get('evidencias') or []))}</td></tr>")
        partes.append("</table>")
    if por_motivo:
        partes.append("<h3 style='margin:16px 0 6px'>Em aberto por motivo</h3><ul style='margin:0;padding-left:18px'>")
        for m in por_motivo:
            rotulo = f"<b>{e(m['rotulo'])}</b>" if m["real"] else e(m["rotulo"])
            partes.append(f"<li>{rotulo}: {m['total']}</li>")
        partes.append("</ul>")
    partes.append(f"<p style='margin:16px 0 0'><a href='{LINK_ABA}'>Abrir o Fechamento no painel</a></p>")
    return assunto, "".join(partes)


def avisar(config: dict, novas: list[dict], rodada: dict, por_motivo: list[dict]) -> bool:
    """Manda o e-mail interno se houver novidade. Nunca levanta: o batimento
    ja gravou, falha de e-mail so loga."""
    if not novas and rodada["fecha"]:
        logger.info("Sem divergencia nova e equacao fecha: sem e-mail.")
        return False
    try:
        from email_utils import COR_DESTAQUE, COR_ERRO, envelope_html, enviar_email
        destino = (config.get("notificacao_execucao") or {}).get("destinatario") or "hugo@freshlogbr.com"
        assunto, corpo = montar_email(novas, rodada, por_motivo)
        ok = enviar_email([destino], assunto,
                          envelope_html(corpo, cor_acento=COR_DESTAQUE if rodada["fecha"] else COR_ERRO),
                          config.get("email", {}))
        logger.info(f"E-mail do batimento {'enviado' if ok else 'NAO enviado'} para {destino}: {assunto}")
        return bool(ok)
    except Exception as ex:  # noqa: BLE001
        logger.exception(f"Falha ao mandar o e-mail do batimento: {ex}")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Batimento diario de pedidos (destino final).")
    ap.add_argument("--data-corte", default=medir.DATA_CORTE_PADRAO.isoformat(), help="YYYY-MM-DD")
    ap.add_argument("--id-minimo", type=int, default=None, help="id da Stokki a partir do qual listar")
    ap.add_argument("--dias-vuupt", type=int, default=30)
    ap.add_argument("--esperar-stokki", type=int, default=1800, help="segundos esperando a vez na Stokki")
    ap.add_argument("--resumo", "--modo-teste", dest="resumo", action="store_true",
                    help="so imprime a equacao, nao grava no banco")
    args = ap.parse_args(argv)

    pedidos, resumo, _contagem, id_minimo = medir.coletar(
        date.fromisoformat(args.data_corte), args.id_minimo, args.dias_vuupt, args.esperar_stokki, DONO_TRAVA)
    medir.logar_resumo(resumo)

    if args.resumo:
        logger.info("--resumo: nada gravado.")
        return 0 if resumo["equacao_fecha"] else 1

    agora = datetime.now()
    conn = banco.conectar(medir.DB_PATH)
    try:
        r = banco.gravar_rodada(conn, pedidos, resumo, id_minimo, agora)
        rodada = consulta.ultima_rodada(conn)
        novas = consulta.novidades(conn, agora.strftime(banco.FMT))
    finally:
        conn.close()
    logger.info(f"Gravado: {r['novos']} novo(s), {r['mudaram']} mudaram, {r['iguais']} iguais, "
                f"{r['congelados']} ja em DESTINO | rodada: {r['totais']} | equacao "
                f"{'FECHA' if r['fecha'] else 'NAO FECHA'} | {len(novas)} divergencia(s) nova(s)")

    with open(medir.CONFIG_PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    avisar(config, novas, rodada, consulta.fechamento(db_path=medir.DB_PATH)["por_motivo"])
    return 0 if r["fecha"] else 1


if __name__ == "__main__":
    sys.exit(main())
