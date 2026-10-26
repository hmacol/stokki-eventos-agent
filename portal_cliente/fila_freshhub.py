# -*- coding: utf-8 -*-
"""
portal_cliente/fila_freshhub.py

Worker da fila `stokki` do sistema novo (plano 1.3 do freshhub, Task 7; ADR-0011). O portal novo recebe o
pedido, guarda o XML original e enfileira; aqui o legado baixa o XML pela API, confere a area (mesma regra
do portal antigo, classificar_area_envio), aplica a regra_xml do embarcador e cria na Stokki pelo MESMO
wizard de importacao do portal antigo (enviar_stokki.executar_wizard, com a trava stokki/sessao_uso), e
devolve o PS ao novo (POST /api/replicacao/{id}/resultado).

Resultado por item:
  criado com PS     -> ok {"ps", "numero_nf"} (o novo troca o codigo FH-n pelo PS)
  fora da area      -> erro {"bloqueio": motivo} (o novo marca "aguardando liberacao"; a equipe libera)
  recusa / duplicado / criado sem PS / falha tecnica -> erro com o texto pra equipe (lista "Aguardando")
Pedido sem NF (modo planilha) e retirada NAO sao automaticos ate a prova na Stokki (Task 7a): voltam como
erro "lance na Stokki e informe o PS". --simular (hml) devolve PS-HML-<n> sem tocar a Stokki.

COMO RODAR (so com freshhub_api no config):
    py -3.11 portal_cliente/fila_freshhub.py --uma-vez [--simular] [--visivel]
"""
import argparse
import logging
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_AQUI = Path(__file__).parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from nucleo import freshhub_client  # noqa: E402
from nucleo.freshhub_client import ErroFreshhub  # noqa: E402

logger = logging.getLogger("portal_envios.freshhub")

LIMITE = 50
MANUAL = "ainda não é automático: lance na Stokki e informe o PS na lista Aguardando a equipe"

# criar_na_stokki(cnpj_emitente, [(nome_arquivo, conteudo, regra_xml)]) ->
#   ({chave_nf: {"criado": bool, "erro": str}}, {numero_nf: "PS-..."})
CriarNaStokki = Callable[[str, list[tuple[str, bytes, str | None]]], tuple[dict[str, dict], dict[str, str]]]


def _devolver(cliente, resumo: dict, id_: str, ok: bool, resultado: dict | None = None,
              erro: str | None = None) -> None:
    try:
        cliente.resultado(id_, ok, resultado=resultado, erro=erro)
    except ErroFreshhub as e:
        # o item fica `enviado` no novo e volta a pendente em 1 h; a Stokki ja pode ter criado: o
        # duplicado na proxima volta vira erro pra equipe (nunca pedido em dobro)
        logger.error(f"fila freshhub: nao consegui devolver o resultado de {id_}: {e}")
        resumo["nao_devolvidos"] += 1


def atender(cliente, *, simular: bool, ler_nfe: Callable[[bytes], dict],
            classificar_area: Callable[[dict], str | None], criar_na_stokki: CriarNaStokki,
            limite: int = LIMITE) -> dict:
    """Um ciclo da fila. Funcoes injetadas: ler_nfe (envio_pedidos.ler_nfe), classificar_area
    (envio_pedidos.classificar_area_envio com o config) e criar_na_stokki (wizard com a trava)."""
    resumo = {"itens": 0, "criados": 0, "bloqueados": 0, "erros": 0, "nao_devolvidos": 0}
    try:
        itens = cliente.pendentes("stokki", limite=limite)
    except ErroFreshhub as e:
        logger.warning(f"fila freshhub: API indisponivel ({e})")
        return resumo
    resumo["itens"] = len(itens)
    grupos: dict[str, list[tuple[dict, dict, bytes]]] = {}
    for item in itens:
        dados = item.get("dados") or {}
        if dados.get("modo") != "xml" or str(dados.get("fluxo") or "").upper() == "RETIRADA":
            resumo["erros"] += 1
            _devolver(cliente, resumo, item["id"], False, erro=MANUAL)
            continue
        try:
            conteudo = cliente.arquivo(item["id"])
            nfe = ler_nfe(conteudo)
        except Exception as e:  # noqa: BLE001 - qualquer falha do arquivo vira erro do item
            resumo["erros"] += 1
            _devolver(cliente, resumo, item["id"], False, erro=f"XML da réplica ilegível: {e}"[:2000])
            continue
        if not dados.get("liberado"):
            motivo = classificar_area(nfe)
            if motivo:
                resumo["bloqueados"] += 1
                _devolver(cliente, resumo, item["id"], False, resultado={"bloqueio": motivo},
                          erro=f"fora da área atendida ({motivo})")
                continue
        grupos.setdefault(str(nfe.get("emitente_cnpj") or ""), []).append((item, nfe, conteudo))

    for cnpj, lote in grupos.items():
        if simular:
            for item, nfe, _c in lote:
                resumo["criados"] += 1
                _devolver(cliente, resumo, item["id"], True, resultado={
                    "ps": f"PS-HML-{nfe.get('numero_nf')}", "numero_nf": nfe.get("numero_nf"), "simulado": True})
            continue
        arquivos = [(f"{nfe['chave_nfe']}.xml", conteudo, (item.get("dados") or {}).get("regra_xml"))
                    for item, nfe, conteudo in lote]
        try:
            por_chave, codigos = criar_na_stokki(cnpj, arquivos)
        except Exception as e:  # noqa: BLE001 - falha tecnica do lote: cada item volta como erro
            logger.error(f"fila freshhub: wizard falhou pro emitente {cnpj}: {e}")
            for item, _n, _c in lote:
                resumo["erros"] += 1
                _devolver(cliente, resumo, item["id"], False, erro=f"Falha ao enviar à Stokki: {e}"[:2000])
            continue
        for item, nfe, _c in lote:
            _resultado_do_item(cliente, resumo, item, nfe, por_chave.get(nfe["chave_nfe"]), codigos)
    logger.info(f"fila freshhub: {resumo}")
    return resumo


def _resultado_do_item(cliente, resumo: dict, item: dict, nfe: dict, r: dict | None, codigos: dict) -> None:
    numero = str(nfe.get("numero_nf") or "")
    if r is None:
        resumo["erros"] += 1
        _devolver(cliente, resumo, item["id"], False, erro="a Stokki não devolveu resultado pra esse XML")
        return
    erro = str(r.get("erro") or "")
    if not r.get("criado"):
        resumo["erros"] += 1
        if "utilizada" in erro.lower():
            _devolver(cliente, resumo, item["id"], False,
                      erro=f"NF {numero} já existe na Stokki: informe o PS na lista Aguardando a equipe")
        else:
            _devolver(cliente, resumo, item["id"], False, erro=f"Stokki recusou: {erro or 'sem detalhe'}"[:2000])
        return
    ps = codigos.get(numero)
    if not ps:
        resumo["erros"] += 1
        _devolver(cliente, resumo, item["id"], False,
                  erro=f"criado na Stokki, mas o PS da NF {numero} não apareceu na listagem: informe o PS")
        return
    resultado = {"ps": ps, "numero_nf": numero}
    if (item.get("dados") or {}).get("dedicado"):
        resultado["dedicado"] = item["dados"]["dedicado"]
    resumo["criados"] += 1
    _devolver(cliente, resumo, item["id"], True, resultado=resultado)


# ------------------------------------------------------------------ ligacao com o portal antigo (producao)


def criar_na_stokki_real(conn, config: dict, headless: bool = True) -> CriarNaStokki:
    """O wizard do portal antigo com a trava de sessao, por emitente (config do embarcador em interno /
    portal_clientes_envio). A regra_xml da replica vence a do cadastro do legado quando vem."""
    import enviar_stokki as es
    import envio_pedidos as ep
    from stokki import sessao_uso

    def _criar(cnpj: str, arquivos: list[tuple[str, bytes, str | None]]):
        cfg = ep.config_stokki_cliente(conn, cnpj, config)
        if not cfg["client_id"]:
            raise RuntimeError("embarcador sem client_id da Stokki (interno.stkkc_id)")
        wiz, px = es.carregar_importador(config)
        with tempfile.TemporaryDirectory(prefix="fh-fila-") as tmp:
            pasta = Path(tmp)
            caminhos = []
            for nome, conteudo, regra in arquivos:
                origem = pasta / f"orig-{nome}"
                origem.write_bytes(conteudo)
                destino = pasta / nome
                es.transformar_xml(regra or cfg["regra_xml"], origem, destino, px)
                caminhos.append(destino)
            espera = sessao_uso.ESPERA_MAXIMA_SEGUNDOS
            if not sessao_uso.adquirir(es.DONO_TRAVA, ttl_segundos=30 * 60, esperar_segundos=espera,
                                       alternativa=True):
                raise RuntimeError(f"Stokki ocupada por '{sessao_uso.em_uso()}'")
            try:
                usuario, senha = es._credenciais(config)
                resultados, _respostas, codigos = es.executar_wizard(cfg, usuario, senha, caminhos,
                                                                     es.PASTA_LOTES / "freshhub", wiz,
                                                                     headless=headless)
            finally:
                sessao_uso.liberar(es.DONO_TRAVA)
        return {Path(r["arquivo"]).stem: r for r in resultados}, codigos

    return _criar


def rodar(config: dict, *, simular: bool, headless: bool = True) -> dict | None:
    """Um ciclo com as pecas de producao. Sem freshhub_api no config: nada (sombra desligada)."""
    import envio_pedidos as ep

    cliente = freshhub_client.de_config(config)
    if cliente is None:
        logger.info("fila freshhub: sem freshhub_api no config, nada a fazer")
        return None
    conn = ep.conectar()
    try:
        return atender(cliente, simular=simular, ler_nfe=lambda conteudo: ep.ler_nfe(conteudo),
                       classificar_area=lambda nfe: ep.classificar_area_envio(nfe, config),
                       criar_na_stokki=criar_na_stokki_real(conn, config, headless=headless))
    finally:
        conn.close()


def main(argv=None) -> int:
    import enviar_stokki as es

    p = argparse.ArgumentParser(description="Worker da fila stokki do sistema novo (freshhub)")
    p.add_argument("--uma-vez", action="store_true", required=True, help="um ciclo")
    p.add_argument("--simular", action="store_true", help="nao toca a Stokki: devolve PS-HML-<n> (hml)")
    p.add_argument("--visivel", action="store_true", help="abre o navegador na tela (debug)")
    args = p.parse_args(argv)
    rodar(es.carregar_config(), simular=args.simular, headless=not args.visivel)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
