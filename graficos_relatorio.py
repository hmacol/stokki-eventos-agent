# -*- coding: utf-8 -*-
"""
graficos_relatorio.py

Geração de gráficos (PNG) para o relatório operacional por e-mail.

E-mail não roda JavaScript nem SVG de forma confiável entre clientes —
a prática recomendada (confirmada via pesquisa: QuickChart, Litmus,
mir.aculo.us) é gerar o gráfico como imagem estática e embutir via
Content-ID, do mesmo jeito que a logo da Freshlog já é embutida em
email_utils.py. Por isso os gráficos aqui são sempre PNG, nunca HTML/JS.

Paleta de status alinhada com a identidade visual da Freshlog
(COR_ACENTO/COR_DESTAQUE de email_utils.py), com uma cor por status
que reflita o significado (laranja = atenção, vermelho = cancelado etc.).
"""
import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # backend sem tela — obrigatório em servidor/headless
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

DPI = 200  # alta resolução (equivalente a "retina") para nitidez no e-mail

CORES_STATUS = {
    "not_assigned": "#F5A623",  # laranja — precisa de atenção
    "accepted":     "#8B5CF6",  # roxo — aceito, aguardando rota
    "on_route":     "#00C896",  # verde-água (acento da marca) — em rota
    "done":         "#10B981",  # verde — concluído
    "canceled":     "#EF4444",  # vermelho — cancelado
    "outros":       "#9CA3AF",  # cinza — status não mapeado
}

_FONTE_ROTULO = {"fontsize": 11, "color": "#1F2937"}


def grafico_status_donut(contagem_rotulada: list[tuple[str, int, str]],
                         caminho_saida: Path) -> Path | None:
    """
    Gráfico de rosca (donut) com a distribuição de pedidos por status.

    contagem_rotulada: lista de (rótulo_exibido, quantidade, cor_hex),
    já filtrada para quantidade > 0 (na ordem em que deve aparecer na
    legenda). Retorna None sem gerar nada se a lista vier vazia — evita
    salvar um gráfico enganoso quando não há pedido nenhum no período.
    """
    if not contagem_rotulada:
        return None

    rotulos = [r for r, _, _ in contagem_rotulada]
    dados   = [q for _, q, _ in contagem_rotulada]
    cores   = [c for _, _, c in contagem_rotulada]
    total   = sum(dados)

    fig, ax = plt.subplots(figsize=(4.6, 4.6), dpi=DPI)
    wedges, _ = ax.pie(
        dados, colors=cores, startangle=90, counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 3},
    )
    ax.text(0, 0.08, str(total), ha="center", va="center",
           fontsize=30, fontweight="bold", color="#141428")
    ax.text(0, -0.16, "pedidos", ha="center", va="center",
           fontsize=12, color="#6B7280")
    ax.legend(
        wedges, [f"{r}  ({d})" for r, d in zip(rotulos, dados)],
        loc="center left", bbox_to_anchor=(1.02, 0.5), frameon=False, fontsize=11,
    )
    ax.set_aspect("equal")
    fig.patch.set_alpha(0)
    fig.tight_layout()
    fig.savefig(caminho_saida, dpi=DPI, transparent=True, bbox_inches="tight")
    plt.close(fig)
    return caminho_saida


def grafico_tendencia_barras(rotulos: list[str], valores: list[int],
                             caminho_saida: Path,
                             indice_destaque: int | None = None) -> Path | None:
    """
    Gráfico de barras verticais (ex: volume de pedidos por dia).

    indice_destaque: posição da barra a destacar com a cor de acento
    (ex: o dia do relatório) — as demais ficam num cinza neutro, pra
    guiar o olho pro dado mais relevante (prática de design "guided
    attention" confirmada na pesquisa de tendências 2026).

    Retorna None sem gerar nada se todos os valores forem 0.
    """
    if not any(valores):
        return None

    cor_destaque = "#00C896"
    cor_neutra   = "#D1D5DB"
    cores = [
        cor_destaque if (indice_destaque is not None and i == indice_destaque) else cor_neutra
        for i in range(len(valores))
    ]

    fig, ax = plt.subplots(figsize=(6.6, 3.0), dpi=DPI)
    barras = ax.bar(rotulos, valores, color=cores, width=0.6)
    limite_topo = max(valores) * 1.25 if max(valores) > 0 else 1
    for barra, valor in zip(barras, valores):
        ax.text(barra.get_x() + barra.get_width() / 2, barra.get_height() + limite_topo * 0.02,
               str(valor), ha="center", va="bottom", **_FONTE_ROTULO, fontweight="bold")

    for lado in ("top", "right", "left"):
        ax.spines[lado].set_visible(False)
    ax.get_yaxis().set_visible(False)
    ax.tick_params(axis="x", labelsize=11, colors="#6B7280", length=0)
    ax.set_ylim(0, limite_topo)
    fig.patch.set_alpha(0)
    fig.tight_layout()
    fig.savefig(caminho_saida, dpi=DPI, transparent=True, bbox_inches="tight")
    plt.close(fig)
    return caminho_saida
