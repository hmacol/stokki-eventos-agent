# -*- coding: utf-8 -*-
"""Relatorio do verificador de botoes: resumo por status e Markdown legivel.
O JSON bruto (resultado) e o que a fase 2 (--consertar) vai consumir."""
from collections import Counter

from verificador_botoes.classificador import STATUS_FALHA

_ORDEM_STATUS = ["ERRO_JS", "ROTA_INEXISTENTE", "LINK_QUEBRADO", "SEM_EFEITO",
                 "NAO_CLICAVEL", "NAO_ENCONTRADO", "NAVEGOU", "OK", "LINK_OK", "PRECISA_DADOS", "DESABILITADO"]


def resumir(resultado: dict) -> dict:
    por_status = Counter()
    for tela in resultado["telas"]:
        for botao in tela.get("botoes", []):
            por_status[botao["status"]] += 1
    total = sum(por_status.values())
    falhas = sum(por_status[s] for s in STATUS_FALHA)
    return {"total": total, "por_status": dict(por_status), "falhas": falhas}


def _ordenar(botoes: list[dict]) -> list[dict]:
    posicao = {s: i for i, s in enumerate(_ORDEM_STATUS)}
    return sorted(botoes, key=lambda b: posicao.get(b["status"], len(_ORDEM_STATUS)))


def _celula(texto) -> str:
    return str(texto or "").replace("|", "\\|").replace("\n", " ")


def gerar_markdown(resultado: dict) -> str:
    resumo = resumir(resultado)
    linhas = [
        f"# Verificador de botoes: {resultado.get('servico', '')}",
        "",
        f"Gerado em {resultado.get('gerado_em', '')}. "
        f"{resumo['total']} botoes verificados, {resumo['falhas']} falhas.",
        "",
        "| Status | Qtde |",
        "|---|---|",
    ]
    for status in _ORDEM_STATUS:
        if resumo["por_status"].get(status):
            linhas.append(f"| {status} | {resumo['por_status'][status]} |")
    linhas.append("")

    for tela in resultado["telas"]:
        linhas.append(f"## {tela['caminho']} ({tela.get('endpoint', '')})")
        linhas.append("")
        if tela.get("pulada"):
            linhas.append(f"Pulada: {tela['pulada']}")
            linhas.append("")
            continue
        botoes = tela.get("botoes", [])
        if not botoes:
            linhas.append("Nenhum botao encontrado.")
            linhas.append("")
            continue
        linhas.append("| Status | Botao | Detalhe |")
        linhas.append("|---|---|---|")
        for botao in _ordenar(botoes):
            descricao = botao["descricao"]
            if botao.get("pai"):
                descricao += f" (via {botao['pai']})"
            linhas.append(f"| {botao['status']} | {_celula(descricao)} | {_celula(botao.get('detalhe'))} |")
        linhas.append("")
    return "\n".join(linhas)
