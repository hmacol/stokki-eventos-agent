# -*- coding: utf-8 -*-
"""
Painel da gravacao da aula da equipe: o app real, com pecas trocadas
pra nada sair da maquina:
  - coleta confirmada: em vez de por o robo de expedicao na fila (Stokki),
    registra uma execucao de mentira; o andamento fica "expedindo" por
    ESPERA_EXPEDICAO_S e devolve o resultado roteirizado (FALHAS_DO_VIDEO:
    uma falha que passa no "Tentar de novo" e um pedido cancelado);
  - retorno confirmado: a copia das fotos no GCS nao roda.
Mesma guarda de rede da aula do cliente (socket so pra loopback, SMTP
recusado); cada tentativa vai pro log e o gravar reprova o video.

    py -3.11 -m aula_equipe.servidor --porta 8098   (de dentro de painel_agentes/)
"""
import argparse
import time
from contextlib import contextmanager

import aula_equipe  # noqa: F401  (poe painel_agentes/ no sys.path)
from aula_portal.servidor import ARQ_BLOQUEIOS, guarda_rede


ESPERA_EXPEDICAO_S = 6   # quanto tempo a tela mostra "Expedindo na Stokki..."
# pedidos do video de Coletas que nao saem de primeira (id_stokki)
FALHAS_DO_VIDEO = {"ids": [92011, 92012, 92014], "tenta_de_novo": 92012, "cancelado": 92014}
_MENSAGENS = {"tenta_de_novo": "A Stokki não respondeu a tempo. Tente de novo.",
              "cancelado": "Pedido cancelado na Stokki: não pode ser expedido."}
_despachos: dict[str, tuple[float, int]] = {}   # token -> (quando, rodada)


def _despachar_roteirizado(conn, coleta_id: int, codigos: list) -> dict:
    from coletas import regras
    rodada = sum(1 for t in _despachos if t.startswith(f"aula-{coleta_id}-")) + 1
    token = f"aula-{coleta_id}-{rodada}"
    _despachos[token] = (time.monotonic(), rodada)
    regras.registrar_execucao(conn, coleta_id, 9000 + rodada, token)
    # pedido cancelado na Stokki some da listagem dela (o ler_stokki marcaria saiu_em na proxima leitura)
    conn.execute("UPDATE expedicao_coletas_fila SET saiu_em = COALESCE(saiu_em, datetime('now', 'localtime')) "
                 "WHERE id_stokki = ?", (FALHAS_DO_VIDEO["cancelado"],))
    conn.commit()
    return {"ok": True, "execucao_id": 9000 + rodada, "na_fila": False}


def _resultado(codigo: str, rodada: int) -> dict:
    if codigo == f"PS-{FALHAS_DO_VIDEO['cancelado']}":
        return {"ok": False, "mensagem": _MENSAGENS["cancelado"]}
    if codigo == f"PS-{FALHAS_DO_VIDEO['tenta_de_novo']}" and rodada == 1:
        return {"ok": False, "mensagem": _MENSAGENS["tenta_de_novo"]}
    return {"ok": True, "mensagem": "Enviado na Stokki"}


def _andamento_roteirizado(_execucao, token: str, _pasta) -> dict:
    quando, rodada = _despachos.get(token, (0.0, 1))
    if time.monotonic() - quando < ESPERA_EXPEDICAO_S:
        return {"ok": True, "estado": "rodando"}
    from aula_equipe import dados   # a regra so aplica nos que estao "expedindo"
    codigos = [f"PS-{f[0]}" for f in dados.FILA_COLETA]
    return {"ok": True, "estado": "fim", "resultados": {c: _resultado(c, rodada) for c in codigos}}


@contextmanager
def roteirizado():
    import painel_agentes
    import retornos
    trocas = [(painel_agentes, "_coletas_despachar", _despachar_roteirizado),
              (painel_agentes, "buscar_execucao", lambda execucao_id: {"id": execucao_id}),
              (painel_agentes.acoes_pedidos, "andamento_expedicao", _andamento_roteirizado),
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
