# -*- coding: utf-8 -*-
"""
coletas/ler_stokki.py

Le a listagem "Aguardando Transportador" da Stokki e mantem
expedicao_coletas_fila (timer a cada 10 min, ver infra/expedicao-coletas-ler.*).
E a fonte da tela /expedicao/coletas e do status Coletado da /pedidos: o
coletor do Galpao 3D para de acompanhar o pedido quando ele chega nesse
estado (Hugo, 12/11).

  - pedido novo na listagem: entra na fila. carga_inicial = 1 so para quem
    estava la na PRIMEIRA leitura (o acumulado, que ainda pode sair pelo botao
    "Expedir na Stokki" da /pedidos sem foto);
  - pedido que sumiu da listagem: saiu_em (virou Enviado; cancelado quem diz
    e o galpao_pedidos);
  - leitura incompleta (erro, Stokki ocupada): nada e gravado, ninguem "some".

  py -3.11 -m coletas.ler_stokki [--modo-teste]
"""
import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from coletas import banco

logger = logging.getLogger("coletas.ler_stokki")

DONO = "coletas-expedicao"
TTL_TRAVA = 300
ESPERA_TRAVA = 120   # a listagem e curta: espera a vez em vez de pular 10 min


def eh_retirada(nome: str, catalogo) -> bool:
    """A regra unica de retirada/coleta no galpao: "CLIENTE RETIRA" no nome ou
    tipo RETIRADA no cadastro de transportadoras."""
    nome = (nome or "").strip()
    if not nome:
        return False
    if "CLIENTE RETIRA" in nome.upper():
        return True
    try:
        return catalogo is not None and catalogo.resolver(nome).tipo == "RETIRADA"
    except Exception:
        return False


def _limpar(texto) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(texto or ""))).strip()


def _listar_stokki(config, conta):
    from stokki.auth import StokkiSession
    from stokki.pedidos import STATUS_AGUARDANDO_TRANSPORTADOR, extrair_id_da_linha, iterar_todos_pedidos
    sessao = StokkiSession(config, conta=conta)
    for linha in iterar_todos_pedidos(sessao, status=STATUS_AGUARDANDO_TRANSPORTADOR):
        id_ = extrair_id_da_linha(linha)
        if id_:
            yield id_, _limpar(linha.get("client")), _limpar(linha.get("carrier"))


def _catalogo_padrao():
    try:
        from regras.transportadoras import CatalogoTransportadoras
        return CatalogoTransportadoras.carregar_do_banco(banco.DB_PATH)
    except Exception as e:
        logger.warning("Cadastro de transportadoras indisponivel (%s): so CLIENTE RETIRA conta como retirada.", e)
        return None


def _registrar(conn, agora, resultado, obs=""):
    conn.execute("INSERT OR REPLACE INTO expedicao_coletas_leituras (quando, resultado, obs) VALUES (?, ?, ?)",
                 (agora.strftime(banco.FMT), resultado, (obs or "")[:300]))
    conn.commit()


def _travar_padrao():
    from stokki import sessao_uso
    return sessao_uso.adquirir(DONO, ttl_segundos=TTL_TRAVA, esperar_segundos=ESPERA_TRAVA, alternativa=True)


def _soltar_padrao():
    from stokki import sessao_uso
    sessao_uso.liberar(DONO)


def ler(conn, config, listar=None, catalogo=None, agora=None, travar=None, soltar=None) -> str:
    """Uma leitura. Devolve "ok" | "ocupada" | "erro". listar(config, conta)
    entrega (id_stokki, embarcador, transportadora) de TODA a listagem."""
    agora = agora or datetime.now()
    quando = agora.strftime(banco.FMT)
    if travar is None:
        travar, soltar = _travar_padrao, _soltar_padrao
    conta = travar()
    if not conta:
        _registrar(conn, agora, "ocupada", "Stokki em uso por outro job")
        return "ocupada"
    try:
        vistos = {}
        for id_, embarcador, transportadora in (listar or _listar_stokki)(config, conta):
            vistos[int(id_)] = (embarcador, transportadora)
    except Exception as e:
        logger.exception("leitura da listagem falhou")
        _registrar(conn, agora, "erro", str(e))
        return "erro"
    finally:
        if soltar:
            soltar()
    catalogo = catalogo if catalogo is not None else _catalogo_padrao()
    conhecidos = {r["id_stokki"]: r for r in conn.execute("SELECT id_stokki, saiu_em FROM expedicao_coletas_fila")}
    primeira = not conn.execute("SELECT 1 FROM expedicao_coletas_leituras WHERE resultado = 'ok' LIMIT 1").fetchone()
    for id_, (embarcador, transportadora) in vistos.items():
        retirada = int(eh_retirada(transportadora, catalogo))
        if id_ in conhecidos:   # se tinha saido, voltou
            conn.execute("UPDATE expedicao_coletas_fila SET embarcador = ?, transportadora = ?, retirada = ?,"
                         " visto_ultimo_em = ?, saiu_em = NULL WHERE id_stokki = ?",
                         (embarcador, transportadora, retirada, quando, id_))
        else:
            conn.execute("INSERT INTO expedicao_coletas_fila (id_stokki, codigo, embarcador, transportadora,"
                         " retirada, carga_inicial, visto_primeiro_em, visto_ultimo_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (id_, f"PS-{id_}", embarcador, transportadora, retirada, int(primeira), quando, quando))
    sumidos = [i for i, r in conhecidos.items() if i not in vistos and not r["saiu_em"]]
    conn.executemany("UPDATE expedicao_coletas_fila SET saiu_em = ? WHERE id_stokki = ?",
                     [(quando, i) for i in sumidos])
    _registrar(conn, agora, "ok", f"{len(vistos)} na listagem, {len(sumidos)} sairam")
    return "ok"


def ultima_leitura(conn) -> str | None:
    r = conn.execute("SELECT MAX(quando) FROM expedicao_coletas_leituras WHERE resultado = 'ok'").fetchone()
    return r[0] if r else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Le a listagem Aguardando Transportador da Stokki.")
    parser.add_argument("--modo-teste", action="store_true", help="Le e imprime, sem gravar no dados.db.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    import yaml
    with open(_RAIZ / ("config" + ".yaml"), encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    if args.modo_teste:
        import tempfile
        conn = banco.conectar(Path(tempfile.mkdtemp()) / "teste.db")
    else:
        conn = banco.conectar()
    try:
        resultado = ler(conn, config)
        logger.info("leitura: %s", resultado)
        if args.modo_teste:
            for r in conn.execute("SELECT transportadora, retirada, COUNT(*) n FROM expedicao_coletas_fila "
                                  "GROUP BY 1, 2 ORDER BY n DESC"):
                print(dict(r))
    finally:
        conn.close()
    return 0 if resultado != "erro" else 1


if __name__ == "__main__":
    sys.exit(main())
