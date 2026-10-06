# -*- coding: utf-8 -*-
"""
regras_documentos.py

Regras de "que documento o pedido precisa ter", compartilhadas pelo
romaneio (roteirizacao/gerar_pdf_romaneios.py) e pelo vigia
(documentos_pedido/vigia_documentos.py). Sem imports do projeto: é
importado de fora de documentos_pedido/.

Boleto esperado (decisão do Hugo, 05/10/2026: "quase todos têm boleto,
mas não necessariamente pra todos os clientes"): quem diz é a própria
NF. DANFE com duplicata no quadro FATURA/DUPLICATA = venda a prazo =
tem boleto. Medido em 180 DANFEs reais (05/10): 49 com duplicata e
boleto, 48 com duplicata e sem boleto (NUU, Sotille, Vida Veg -- é a
pendência real), 83 sem o quadro (Quatro Estrelas, Jersey, e a DANFE
que a Dourado manda por e-mail). Sem quadro não dá pra decidir pela NF:
vale a lista de embarcadores que o Hugo disse que mandam boleto.
"""
import re

# sender_id da VUUPT (tabela interno) -> nome. Lista do Hugo, 05/10:
# grupo Marchef, NUU, Dourado, Muai, Ciao (= De Tommaso), Sotille,
# Vida Veg, Jersey Vale. Muai sem sender_id na interno em 05/10:
# acrescentar quando tiver.
SENDERS_BOLETO_SEM_FATURA: dict[int, str] = {
    11426239: "DOURADO",
    20562589: "DE TOMMASO (CIAO)",
    20011781: "JERSEY VALE",
    16362183: "NUU",
    20135423: "VIDAVEG",
    15714169: "AMAZONIKA (SOTILLE)",
    18164791: "MARCHEF - ITAUEIRA",
    21849438: "MARCHEF - JEE",
    18164790: "MARCHEF - ALMAZ",
    18638577: "MARCHEF - GOURMAR",
    21849668: "MARCHEF - MARCHEF",
    21849487: "MARCHEF - 3M",
    19276807: "MARCHEF - COPACAM",
    19445576: "MARCHEF - AOS AREIA",
    19013058: "MARCHEF - FONSECA",
}

_RE_QUADRO_FATURA = re.compile(r"FATURA\s*/?\s*DUPLICATA", re.IGNORECASE)
# O quadro termina onde começa o cálculo do imposto (todos os layouts vistos)
_RE_FIM_QUADRO = re.compile(r"C[ÁA]LCULO\s+DO\s+IMPOSTO|BASE\s+DE\s+C[ÁA]LCULO", re.IGNORECASE)
# Uma duplicata: "Venc. 21/10/2026" (NFePHP/Olist) ou "001 31/08/2026 1.342,86" (tabela)
_RE_DUPLICATA = re.compile(
    r"(?:Venc\.?|Vencimento)\s*\d{2}/\d{2}/\d{4}"
    r"|\d{2}/\d{2}/\d{4}\s*(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}",
    re.IGNORECASE,
)


def cobranca_da_danfe(texto: str) -> int | None:
    """1 = quadro de fatura com duplicata; 0 = quadro sem duplicata;
    None = DANFE sem o quadro (ou texto vazio)."""
    m = _RE_QUADRO_FATURA.search(texto or "")
    if not m:
        return None
    resto = texto[m.end():]
    fim = _RE_FIM_QUADRO.search(resto)
    quadro = resto[:fim.start()] if fim else resto[:400]
    return 1 if _RE_DUPLICATA.search(quadro) else 0


def boleto_esperado(sender_id: int | None, cobrancas: list[int | None]) -> bool:
    """cobrancas: o valor de cobranca_da_danfe de cada NF do pedido
    (lista vazia = pedido sem NF). -1 (gravado pelo backfill) = sem quadro."""
    cobrancas = [None if c == -1 else c for c in cobrancas]
    if any(c == 1 for c in cobrancas):
        return True
    if cobrancas and all(c == 0 for c in cobrancas):
        return False
    return sender_id in SENDERS_BOLETO_SEM_FATURA
