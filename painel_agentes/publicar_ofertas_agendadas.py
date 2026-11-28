# -*- coding: utf-8 -*-
"""
painel_agentes/publicar_ofertas_agendadas.py

Publica, na hora escolhida no card do /planejamento ("Publicar para motoristas"
com horario, Hugo 25/11), as rotas em edicao sem motorista: mesmo caminho do botao
(planejamento_rotas.publicar_oferta_rascunho + push pra VPS + aviso aos elegiveis).
Falha (ex.: ganhou motorista, ninguem elegivel) fica no card em oferta_agendada_erro.

Timer a cada minuto (infra/stokki-publicar-ofertas-agendadas.*):
    venv/bin/python painel_agentes/publicar_ofertas_agendadas.py
    venv/bin/python painel_agentes/publicar_ofertas_agendadas.py --modo-teste   # so mostra o que publicaria
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", Path(__file__).parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger("publicar_ofertas_agendadas")


def rodar(modo_teste: bool = False) -> dict:
    import rascunhos_rota
    vencidas = rascunhos_rota.listar_ofertas_agendadas_vencidas()
    stats = {"vencidas": len(vencidas), "publicadas": 0, "falhas": 0}
    if not vencidas:
        return stats
    import planejamento_rotas
    from avisar_motoristas_rotas import notificar_oferta_motoristas, push_ofertas_vps
    config = planejamento_rotas._carregar_config()
    for r in vencidas:
        if modo_teste:
            print(f"[modo teste] publicaria rascunho {r['id']} ({r['nome']}), agendado para {r['oferta_agendada_em']}")
            continue
        rascunhos_rota.baixar_agendamento_oferta(r["id"])      # antes de publicar: nunca publica duas vezes
        try:
            res = planejamento_rotas.publicar_oferta_rascunho(r["id"])
        except Exception as exc:
            logger.exception("Rascunho %s: falha ao publicar no horario agendado", r["id"])
            res = {"ok": False, "erro": f"erro inesperado: {exc}"}
        if not res["ok"]:
            rascunhos_rota.baixar_agendamento_oferta(r["id"], res["erro"])
            stats["falhas"] += 1
            logger.warning("Rascunho %s nao publicado no horario agendado: %s", r["id"], res["erro"])
            continue
        stats["publicadas"] += 1
        try:
            push_ofertas_vps(config.get("confirmacao_rotas", {}))
            aviso = notificar_oferta_motoristas(res["elegiveis"], date.fromisoformat(r["data_alvo"]), config)
            logger.info("Rascunho %s publicado no horario agendado para %d elegivel(is); aviso: %s",
                        r["id"], len(res["elegiveis"]), aviso)
        except Exception:
            logger.exception("Rascunho %s publicado, mas o aviso aos motoristas falhou", r["id"])
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publica as ofertas de rota agendadas no /planejamento.")
    parser.add_argument("--modo-teste", action="store_true", help="mostra o que publicaria, sem publicar nem avisar")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    stats = rodar(args.modo_teste)
    (logger.info if stats["vencidas"] else logger.debug)("Concluido: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
