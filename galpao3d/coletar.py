# -*- coding: utf-8 -*-
"""
galpao3d/coletar.py

Coletor do Galpao 3D (timer a cada 1 min, ver infra/galpao3d-coletar.*).
  - Stokki: lista os pedidos abertos (Open, Separating, Ready to Pack, On hold);
    a situacao de todos vem da listagem. Pedido novo em Open so e gravado (sem
    show). A pagina show e aberta SO para o que sumiu dos abertos (pulou
    etapas), mudou de situacao ou ja chegou fora de Open; ordem: sumidos,
    depois Ready to Pack, depois Separating, depois o resto. Maximo de 40
    shows por rodada; show que falha 3 vezes seguidas (ou da 404) e
    abandonada ate a proxima mudanca na listagem. Prioridade MINIMA:
    a sessao e compartilhada com jobs de producao, entao so entra se estiver
    livre e sem fila (esperar_segundos=0), na conta principal, sem reserva.
  - Vuupt: resumo das rotas de hoje (carros na rua, entregues, insucessos).
Sempre sai 0, salvo erro inesperado.

  py -3.11 -m galpao3d.coletar [--modo-teste] [--forcar]
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from galpao3d import banco
from galpao3d.historico_stokki import ESTADO_API_PARA_SITUACAO, ler_historico
from galpao3d.turno import deve_coletar
from stokki import sessao_uso
from stokki.cancelar import URL_SHOW, _limpar
from stokki.pedidos import contar_pedidos, extrair_id_da_linha, iterar_todos_pedidos

logger = logging.getLogger("galpao3d.coletar")

DONO = "galpao-3d"              # timer
DONO_TESTE = "galpao-3d-teste"  # execucao manual (--modo-teste/--forcar): nao libera a trava do timer
TTL_TRAVA = 120
ORCAMENTO_SEGUNDOS = 45   # tempo maximo segurando a Stokki por rodada
RETENCAO_DIAS = 7         # galpao_rua/contagens/coletas (1 linha por minuto)
ESTADOS_ABERTOS = ("Open", "Separating", "Ready to Pack", "On hold")
SITUACOES_ABERTAS = {ESTADO_API_PARA_SITUACAO[e] for e in ESTADOS_ABERTOS}
MAX_SHOWS = 40
MAX_FALHAS_SHOW = 3
SIT_ABERTO = ESTADO_API_PARA_SITUACAO["Open"]
# prioridade na fila de shows (menor primeiro; sumidos vem antes de todos)
_PRIORIDADE_SHOW = {ESTADO_API_PARA_SITUACAO["Ready to Pack"]: 0, ESTADO_API_PARA_SITUACAO["Separating"]: 1}
# Situacao quando o pedido sumiu dos abertos mas a linha do tempo ainda termina
# em situacao aberta (evita reabrir a show a cada rodada).
SITUACAO_FORA = "Fora dos abertos"
# Status de rota da Vuupt que contam como "carro na rua" (os mesmos de
# nucleo.sincronizar_vuupt._STATUS_ROTA_VUUPT que viram ROTA_EM_ROTA).
STATUS_NA_RUA = {"on_route", "in_progress", "started"}


def _carregar_config() -> dict:
    import yaml
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _log(conn, fonte, agora, resultado, obs=""):
    conn.execute("INSERT INTO galpao_coletas (fonte, quando, resultado, obs) VALUES (?, ?, ?, ?)",
                 (fonte, agora.strftime(banco.FMT), resultado, (obs or "")[:500]))
    conn.commit()


def _sessao_padrao(config, conta):
    from stokki.auth import StokkiSession
    return StokkiSession(config, conta=conta)


def _atualizar_pela_listagem(conn, abertos: dict, agora: datetime) -> list:
    """Grava a situacao de todos os abertos a partir da listagem e devolve a fila
    de shows [(id, codigo, embarcador, situacao, sumiu)] na ordem de prioridade."""
    quando = agora.strftime(banco.FMT)
    conhecidos = {r["id_stokki"]: r for r in conn.execute("SELECT * FROM galpao_pedidos")}
    for id_, (codigo, embarcador, situacao) in abertos.items():
        antigo = conhecidos.get(id_)
        if antigo is None:   # novo: em Open nao mudou de etapa, so grava
            conn.execute("INSERT INTO galpao_pedidos (id_stokki, codigo, embarcador, situacao, atualizado_em,"
                         " falhas_show, show_pendente) VALUES (?, ?, ?, ?, ?, 0, ?)",
                         (id_, codigo, embarcador, situacao, quando, int(situacao != SIT_ABERTO)))
        elif antigo["situacao"] != situacao:   # mudou: show de novo, zera as falhas
            conn.execute("UPDATE galpao_pedidos SET codigo = ?, embarcador = ?, situacao = ?, atualizado_em = ?,"
                         " falhas_show = 0, show_pendente = 1 WHERE id_stokki = ?",
                         (codigo, embarcador, situacao, quando, id_))
    conn.commit()
    # sumiu dos abertos: estava num estado aberto e nao esta mais na listagem
    sumidos = [(i, r["codigo"], r["embarcador"], r["situacao"], True) for i, r in conhecidos.items()
               if r["situacao"] in SITUACOES_ABERTAS and i not in abertos]
    pendentes = [(r["id_stokki"], r["codigo"], r["embarcador"], r["situacao"], False)
                 for r in conn.execute("SELECT * FROM galpao_pedidos WHERE show_pendente = 1")
                 if r["id_stokki"] in abertos]
    pendentes.sort(key=lambda x: _PRIORIDADE_SHOW.get(x[3], 2))
    return sumidos + pendentes


def _falha_da_show(conn, id_: int, sumiu: bool, desistir: bool) -> None:
    """Conta a falha; na 3a seguida (ou 404) desiste ate nova mudanca na listagem."""
    conn.execute("UPDATE galpao_pedidos SET falhas_show = COALESCE(falhas_show, 0) + 1 WHERE id_stokki = ?", (id_,))
    n = conn.execute("SELECT falhas_show FROM galpao_pedidos WHERE id_stokki = ?", (id_,)).fetchone()[0]
    if desistir or n >= MAX_FALHAS_SHOW:
        if sumiu:   # fora da listagem: para de aparecer como sumido
            conn.execute("UPDATE galpao_pedidos SET situacao = ?, show_pendente = 0 WHERE id_stokki = ?",
                         (SITUACAO_FORA, id_))
        else:       # ainda na listagem: situacao continua a da listagem
            conn.execute("UPDATE galpao_pedidos SET show_pendente = 0 WHERE id_stokki = ?", (id_,))
    conn.commit()


def coletar_stokki(conn, config, sessao_fabrica=None, agora=None, relogio=None, dono=DONO) -> str:
    agora = agora or datetime.now()
    relogio = relogio or time.monotonic
    fabrica = sessao_fabrica or _sessao_padrao
    conta = sessao_uso.adquirir(dono, ttl_segundos=TTL_TRAVA, esperar_segundos=0, alternativa=True)
    if not conta:
        _log(conn, "stokki", agora, "ocupada", "Stokki em uso por outro job")
        return "ocupada"
    inicio = relogio()

    def motivo_para_ceder():
        """Producao tem prioridade: fila na conta ou tempo estourado = parar."""
        if sessao_uso.fila(conta):
            return "cedeu a vez (fila na Stokki)"
        if relogio() - inicio > ORCAMENTO_SEGUNDOS:
            return f"orcamento de {ORCAMENTO_SEGUNDOS}s estourado"
        return None

    try:
        sessao = fabrica(config, conta)
        contagens = contar_pedidos(sessao, "all")
        abertos = {}   # id -> (codigo, embarcador, situacao)
        conn.execute("INSERT OR REPLACE INTO galpao_contagens (quando, json) VALUES (?, ?)",
                     (agora.strftime(banco.FMT), json.dumps(contagens, ensure_ascii=False, default=str)))
        conn.commit()
        for estado in ESTADOS_ABERTOS:
            motivo = motivo_para_ceder()
            if motivo:   # listagem incompleta: nao compara nem fecha ninguem ("parcial" nao conta pra idade)
                _log(conn, "stokki", agora, "parcial", f"{motivo} antes de listar {estado}")
                return "parcial"
            # iterar_todos_pedidos entrega UMA linha (dict) por vez
            for linha in iterar_todos_pedidos(sessao, status=estado):
                id_ = extrair_id_da_linha(linha)
                if id_ and id_ not in abertos:
                    abertos[id_] = (f"PS-{id_}", _limpar(str(linha.get("client", ""))),
                                    ESTADO_API_PARA_SITUACAO[estado])
        # chegou aqui = as 4 listagens completas (se uma falhar, cai no except e ninguem "some")
        fila = _atualizar_pela_listagem(conn, abertos, agora)
        shows = falhas = 0
        cedeu = None
        for id_, codigo, embarcador, situacao, sumiu in fila[:MAX_SHOWS]:
            cedeu = motivo_para_ceder()
            if cedeu:
                break
            status = None
            try:
                resp = sessao.get(URL_SHOW.format(id=id_))
                status = getattr(resp, "status_code", None)
                if hasattr(resp, "raise_for_status"):
                    resp.raise_for_status()
                hist = ler_historico(resp.text)
            except Exception as e:
                falhas += 1
                logger.warning("show %s falhou: %s", id_, e)
                _falha_da_show(conn, id_, sumiu, desistir=status == 404)
                continue
            shows += 1
            if sumiu:
                ultima = hist["linha_do_tempo"][-1]["situacao"] if hist["linha_do_tempo"] else ""
                situacao = ultima if ultima and ultima not in SITUACOES_ABERTAS else SITUACAO_FORA
            banco.gravar_historico(conn, id_, codigo, embarcador, situacao, hist)
            conn.execute("UPDATE galpao_pedidos SET falhas_show = 0, show_pendente = 0 WHERE id_stokki = ?", (id_,))
            conn.commit()
        obs = f"abertos={len(abertos)} shows={shows} falhas={falhas} pendentes={len(fila) - shows - falhas}"
        if cedeu:
            obs += f" -- {cedeu}"
        _log(conn, "stokki", agora, "ok", obs)
        logger.info("stokki ok: %s", obs)
        return "ok"
    except Exception as e:
        logger.exception("coleta Stokki falhou")
        _log(conn, "stokki", agora, "erro", f"{type(e).__name__}: {e}")
        return "erro"
    finally:
        sessao_uso.liberar(dono)


def _nome_motorista(rota: dict) -> str:
    agente = rota.get("agent")
    if isinstance(agente, dict) and agente.get("name"):
        return agente["name"]
    return rota.get("agent_name") or str(rota.get("agent_id") or "")


def coletar_vuupt(conn, config, listar=None, agora=None) -> str:
    agora = agora or datetime.now()
    try:
        from nucleo import banco as nb
        from nucleo.sincronizar_vuupt import _listar_rotas_do_dia, extrair_servicos, situacao_do_servico
        listar = listar or _listar_rotas_do_dia
        rotas, na_rua, entregues, insucessos = [], 0, 0, 0
        for r in listar(config["vuupt_api"]["token"], agora.date()):
            sit = [situacao_do_servico(s) for s in extrair_servicos(r)]
            e, i = sit.count(nb.PARADA_ENTREGUE), sit.count(nb.PARADA_INSUCESSO)
            c = sit.count(nb.PARADA_CANCELADA)
            status = (r.get("status") or "").lower()
            na_rua += status in STATUS_NA_RUA
            entregues += e
            insucessos += i
            rotas.append({"nome": r.get("name", ""), "motorista": _nome_motorista(r), "status": status,
                          "total": len(sit), "entregues": e, "insucessos": i, "pendentes": len(sit) - e - i - c})
        resumo = {"rotas": rotas, "carros_na_rua": na_rua, "entregues": entregues, "insucessos": insucessos}
        conn.execute("INSERT OR REPLACE INTO galpao_rua (quando, json) VALUES (?, ?)",
                     (agora.strftime(banco.FMT), json.dumps(resumo, ensure_ascii=False)))
        _log(conn, "vuupt", agora, "ok", f"rotas={len(rotas)} na_rua={na_rua}")
        return "ok"
    except Exception as e:
        logger.exception("coleta Vuupt falhou")
        _log(conn, "vuupt", agora, "erro", f"{type(e).__name__}: {e}")
        return "erro"


def limpar_antigos(conn, agora) -> None:
    """Tabelas de 1 linha por minuto: guarda so os ultimos RETENCAO_DIAS dias."""
    limite = (agora - timedelta(days=RETENCAO_DIAS)).strftime(banco.FMT)
    for tabela in ("galpao_rua", "galpao_contagens", "galpao_coletas"):
        conn.execute(f"DELETE FROM {tabela} WHERE quando < ?", (limite,))
    conn.commit()


def main(argv=None, agora=None) -> int:
    parser = argparse.ArgumentParser(description="Coletor do Galpao 3D (Stokki + Vuupt).")
    parser.add_argument("--modo-teste", action="store_true", help="Coleta e imprime, sem gravar.")
    parser.add_argument("--forcar", action="store_true", help="Ignora a janela de coleta.")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    agora = agora or datetime.now()
    if not args.forcar and not deve_coletar(agora):
        logger.info("fora da janela de coleta (%s)", agora.strftime("%H:%M"))
        return 0
    config = _carregar_config()
    if args.modo_teste:
        import tempfile
        conn = banco.conectar(Path(tempfile.mkdtemp()) / "teste.db")   # banco descartavel: nada vai pro dados.db
    else:
        conn = banco.conectar()
    try:
        dono = DONO_TESTE if (args.modo_teste or args.forcar) else DONO
        r_s = coletar_stokki(conn, config, agora=agora, dono=dono)
        r_v = coletar_vuupt(conn, config, agora=agora)
        logger.info("stokki=%s vuupt=%s", r_s, r_v)
        if not args.modo_teste:
            limpar_antigos(conn, agora)
        if args.modo_teste:
            for t in ("galpao_pedidos", "galpao_eventos", "galpao_contagens", "galpao_rua", "galpao_coletas"):
                print(t, [dict(x) for x in conn.execute(f"SELECT * FROM {t} LIMIT 20")])
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
