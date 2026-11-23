"""Sonda unica (somente leitura) das situacoes da Stokki, para o Galpao 3D.

Para cada `state` da API de pedidos de saida, lista 3 pedidos, imprime
id + situacao da lista e salva o HTML da pagina "show" do primeiro pedido em
/tmp/galpao3d_show_<state>.html. Imprime as chaves/numeros de contar_pedidos.
So faz GET. Rodar na VPS:
    cd /opt/stokki-eventos && sudo -u www-data venv/bin/python /tmp/sondar.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, "/opt/stokki-eventos")
sys.path.insert(0, "/opt/stokki-eventos/portal_cliente")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from stokki import sessao_uso
from stokki.auth import StokkiSession
from stokki.cancelar import URL_SHOW, _limpar
from stokki.pedidos import contar_pedidos, extrair_id_da_linha, listar_pedidos

DONO = "galpao-3d-sonda"
ESTADOS = ("Open", "Separating", "Ready to Pack", "On hold", "Waiting for Carrier")

CHAVE_NFE_FALSA = "35000000000000000000550010000000001000000001"


def limpar_captura(html: str) -> str:
    """Tira de uma pagina "show" o que identifica cliente/sessao: chave de
    NF-e (tem o CNPJ do emitente), tokens, links de download/xml, data-url e
    o link publico de rastreio."""
    html = re.sub(r"\d{44}", CHAVE_NFE_FALSA, html)
    html = re.sub(r'(name="csrf-token" content=")[^"]*"', r'\g<1>XXX"', html)
    html = re.sub(r'(name="_token" value=")[^"]*"', r'\g<1>XXX"', html)
    html = re.sub(r'((?:href|data-url|data-href|src)=)"[^"]*(?:download|/xml|/document/)[^"]*"', r'\g<1>"#"', html)
    html = re.sub(r'(data-[a-z-]*url=)"[^"]*"', r'\g<1>"#"', html)
    html = re.sub(r'value="https?://[^"]*/order/tracking/[^"]*"', 'value="#"', html)
    return html


def carregar_config() -> dict:
    from enviar_stokki import carregar_config as _carregar
    return _carregar()


def main() -> int:
    conta = sessao_uso.adquirir(DONO, esperar_segundos=300)
    if not conta:
        print(f"Stokki ocupada por: {sessao_uso.em_uso()}")
        return 0
    try:
        sessao = StokkiSession(carregar_config(), conta=conta)
        for state in ESTADOS:
            dados = listar_pedidos(sessao, status=state, por_pagina=3,
                                   ordenar_coluna="1", ordenar_dir="desc")
            linhas = dados.get("aaData", [])
            print(f"== {state}: {len(linhas)} linha(s)")
            primeiro = None
            for linha in linhas:
                id_ = extrair_id_da_linha(linha)
                print(f"  id={id_} state={_limpar(linha.get('state', ''))}")
                if primeiro is None and id_:
                    primeiro = id_
            if primeiro:
                resp = sessao.get(URL_SHOW.format(id=primeiro))
                resp.raise_for_status()
                caminho = f"/tmp/galpao3d_show_{state.replace(' ', '_')}.html"
                with open(caminho, "w", encoding="utf-8") as f:
                    f.write(limpar_captura(resp.text))
                print(f"  salvo {caminho} (id {primeiro})")
        print("== contar_pedidos(all)")
        for k, v in contar_pedidos(sessao, "all").items():
            print(f"  {k}: {v}")
    finally:
        sessao_uso.liberar(DONO)
    return 0


if __name__ == "__main__":
    sys.exit(main())
