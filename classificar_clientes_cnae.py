# -*- coding: utf-8 -*-
"""
classificar_clientes_cnae.py

Consulta o CNAE de cada destinatario da tabela `clientes` na BrasilAPI
(dados publicos da Receita) e grava a categoria em `clientes_cnae` --
ver regras/categoria_cliente.py (pedido do Hugo, 29/09).

So consulta quem ainda nao tem categoria: a primeira carga leva ~1h
(1 consulta por segundo), as seguintes so pegam cliente novo e quem deu
erro na vez anterior. CPF vira "pessoa_fisica" sem consulta. Correcao
manual nunca e sobrescrita. Roda sob demanda (nao e rotina agendada).

COMO USAR:
    py -3.11 classificar_clientes_cnae.py --modo-teste --limite 20   (consulta e mostra, nao grava)
    py -3.11 classificar_clientes_cnae.py                            (carga de verdade)
    py -3.11 classificar_clientes_cnae.py --definir 00000000000191 supermercado
"""
import argparse
import collections
import logging
import sqlite3
import sys
import time
from pathlib import Path

import requests

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from http_retry import chamar_com_retry
from regras import categoria_cliente as cc

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CAMINHO_BANCO = RAIZ / "dados" / "dados.db"
URL_CNPJ = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
PAUSA_SEGUNDOS = 1.0


def consultar_cnpj(cnpj: str) -> tuple[dict | None, str | None]:
    """(resposta, erro): um dos dois vem None."""
    try:
        resp = chamar_com_retry(
            requests.get, URL_CNPJ.format(cnpj=cnpj),
            headers={"User-Agent": "freshhub-classificar-cnae/1.0"}, timeout=20,
        )
    except requests.RequestException as e:
        return None, f"rede: {e}"
    if resp.status_code != 200:
        return None, f"http {resp.status_code}"
    return resp.json(), None


def executar(modo_teste: bool = False, limite: int | None = None) -> dict:
    if modo_teste:
        conn = sqlite3.connect(f"file:{CAMINHO_BANCO}?mode=ro", uri=True)
        tem_tabela = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'clientes_cnae'").fetchone()
        resolvidos = cc.documentos_ja_resolvidos(conn) if tem_tabela else set()
    else:
        conn = sqlite3.connect(CAMINHO_BANCO)
        cc.criar_tabela(conn)
        resolvidos = cc.documentos_ja_resolvidos(conn)

    contagem = collections.Counter()
    try:
        documentos = sorted({
            cc._so_digitos(r[0]) for r in conn.execute("SELECT cnpj FROM clientes")
        } - resolvidos - {""})
        logger.info(f"{len(documentos)} documento(s) sem categoria ({len(resolvidos)} ja resolvidos).")

        consultas = 0
        for doc in documentos:
            tipo = cc.tipo_documento(doc)
            if tipo == "cpf":
                if not modo_teste:
                    cc.gravar_pessoa_fisica(conn, doc)
                contagem["pessoa_fisica"] += 1
                continue
            if tipo == "invalido":
                contagem["documento invalido"] += 1
                continue

            if limite is not None and consultas >= limite:
                contagem["fora do limite"] += 1
                continue
            if consultas:
                time.sleep(PAUSA_SEGUNDOS)
            consultas += 1

            resposta, erro = consultar_cnpj(doc)
            if erro == "http 404" and tipo == "ambiguo":
                # fecha como CNPJ e como CPF, e a Receita nao conhece: e CPF
                if not modo_teste:
                    cc.gravar_pessoa_fisica(conn, doc)
                contagem["pessoa_fisica"] += 1
                continue
            if erro:
                logger.warning(f"{doc}: {erro}")
                if not modo_teste:
                    cc.gravar_erro(conn, doc, erro)
                contagem[f"erro ({erro.split(':')[0]})"] += 1
                continue

            if modo_teste:
                secundarios = [s.get("codigo") for s in (resposta.get("cnaes_secundarios") or [])]
                categoria, origem = cc.categoria_por_cnae(resposta.get("cnae_fiscal"), secundarios)
                logger.info(f"{doc}: {categoria} ({origem}) -- {resposta.get('cnae_fiscal')} "
                            f"{resposta.get('cnae_fiscal_descricao')}")
            else:
                categoria, origem = cc.gravar_consulta(conn, doc, resposta)
            contagem[categoria] += 1
            if consultas % 100 == 0:
                logger.info(f"{consultas} consulta(s) feitas...")
    finally:
        conn.close()

    logger.info(("MODO TESTE, nada gravado. " if modo_teste else "") + "Resultado: "
                + ", ".join(f"{k}={v}" for k, v in contagem.most_common()))
    return dict(contagem)


def main():
    parser = argparse.ArgumentParser(description="Classifica destinatarios pelo CNAE.")
    parser.add_argument("--modo-teste", action="store_true", help="consulta e mostra, nao grava")
    parser.add_argument("--limite", type=int, help="maximo de consultas a Receita nesta rodada")
    parser.add_argument("--definir", nargs=2, metavar=("DOCUMENTO", "CATEGORIA"),
                        help=f"correcao manual; categorias: {', '.join(cc.CATEGORIAS)}")
    args = parser.parse_args()

    if args.definir:
        conn = sqlite3.connect(CAMINHO_BANCO)
        try:
            cc.criar_tabela(conn)
            cc.definir_categoria_manual(conn, args.definir[0], args.definir[1])
        finally:
            conn.close()
        logger.info(f"{args.definir[0]} -> {args.definir[1]} (manual).")
        return

    executar(modo_teste=args.modo_teste, limite=args.limite)


if __name__ == "__main__":
    main()
