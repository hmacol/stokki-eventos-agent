# -*- coding: utf-8 -*-
"""
Painel da gravacao da aula da equipe: o app real, com duas pecas trocadas
pra nada sair da maquina:
  - coleta confirmada: em vez de por o robo de expedicao na fila (Stokki),
    marca os pedidos como expedidos na hora;
  - retorno confirmado: a copia das fotos no GCS nao roda.
Mesma guarda de rede da aula do cliente (socket so pra loopback, SMTP
recusado); cada tentativa vai pro log e o gravar reprova o video.

    py -3.11 -m aula_equipe.servidor --porta 8098   (de dentro de painel_agentes/)
"""
import argparse
from contextlib import contextmanager

import aula_equipe  # noqa: F401  (poe painel_agentes/ no sys.path)
from aula_portal.servidor import ARQ_BLOQUEIOS, guarda_rede


def _despachar_simulado(conn, coleta_id: int, codigos: list) -> dict:
    from coletas import regras
    regras.aplicar_resultados(conn, coleta_id, {c: {"ok": True, "mensagem": "Enviado na Stokki"} for c in codigos})
    return {"ok": True}


@contextmanager
def roteirizado():
    import painel_agentes
    import retornos
    trocas = [(painel_agentes, "_coletas_despachar", _despachar_simulado),
              (retornos, "enviar_gcs", lambda *_a, **_k: None)]
    antigos = [(m, nome, getattr(m, nome)) for m, nome, _ in trocas]
    for m, nome, novo in trocas:
        setattr(m, nome, novo)
    try:
        yield
    finally:
        for m, nome, velho in antigos:
            setattr(m, nome, velho)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, required=True)
    porta = ap.parse_args().porta
    with guarda_rede(ARQ_BLOQUEIOS):
        import painel_agentes
        painel_agentes.app.config["SESSION_COOKIE_SECURE"] = False   # http://127.0.0.1: o cookie Secure nao voltaria
        with roteirizado():
            painel_agentes.app.run(host="127.0.0.1", port=porta, threaded=True)


if __name__ == "__main__":
    main()
