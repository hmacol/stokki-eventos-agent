# -*- coding: utf-8 -*-
"""
documentos_pedido/localizar_arquivos.py

Resolve o `nome_arquivo` registrado em documentos_processados (SQLite)
para o arquivo físico em disco. Os PDFs processados NÃO são apagados
depois do upload pro GCS -- continuam nas 3 pastas temporárias abaixo,
o que permite montar PDFs derivados (ex: romaneio por rota, ver
roteirizacao/gerar_pdf_romaneios.py) sem baixar nada da nuvem.

Sem imports de outros módulos do projeto de propósito: este módulo é
importado de fora de documentos_pedido/ (com cwd diferente), e não
pode arrastar efeitos colaterais junto.
"""
from pathlib import Path

_PASTA_DADOS = Path(__file__).parent / "dados"

# Ordem de busca: mesma precedência do fluxo que gerou os arquivos --
# downloads da Stokki primeiro (maioria), boletos/NFs já separados dos
# PDFs consolidados, e por último anexos brutos de e-mail.
PASTAS_BUSCA = [
    _PASTA_DADOS / "downloads_stokki_temp",
    _PASTA_DADOS / "boletos_separados",
    _PASTA_DADOS / "nfs_separadas",
    _PASTA_DADOS / "anexos_temp",
]


def resolver_arquivo_local(nome_arquivo: str) -> Path | None:
    """
    Devolve o Path do primeiro arquivo com esse nome nas pastas de
    busca, ou None se não estiver em nenhuma (aí o chamador decide:
    registrar pendência, tentar GCS etc).
    """
    if not nome_arquivo:
        return None
    for pasta in PASTAS_BUSCA:
        candidato = pasta / nome_arquivo
        if candidato.is_file():
            return candidato
    return None
