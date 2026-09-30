# -*- coding: utf-8 -*-
"""
regras/resumo_oferta.py

Resumo de uma rota (ainda em rascunho) mostrado ao motorista no
marketplace de escolha aberta (Hugo, 22/08) -- nunca a rota inteira
(endereço/remetente/destinatário de cada parada), só o suficiente pra
ele decidir: região/cidades/bairros por onde passa, paradas por nível
de dificuldade, caixas, peso aproximado quando já é dado real (nunca
estimativa -- ver roteirizacao/gerar_pdf_romaneios.calcular_peso_paradas).

Fonte: paradas de um rascunho (painel_agentes/rascunhos_rota.py, ainda
pré-Vuupt) -- reaproveita os mesmos motores já usados pro aviso de rota
de hoje (avisar_motoristas_rotas._zona_da_rota) e pro romaneio
(gerar_pdf_romaneios), em vez de duplicar classificação de zona/viagem
ou extração de peso.
"""
import re
import sys
from collections import Counter
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

_RE_UF_FINAL = re.compile(r"(.+?)\s*-\s*[A-Z]{2}$")


def _servico_fake(parada: dict) -> dict:
    """Adapta uma linha de rascunhos_parada (chave 'endereco') pro
    formato que classificar_rota_viagem/classificar_rota_zona/
    _zona_da_rota esperam (serviço bruto da Vuupt, chave 'address') --
    mesmo formato mínimo já usado em planejamento_rotas.alocar_motoristas_rascunhos.
    Latitude/longitude já resolvidas em rascunhos_parada NÃO entram aqui
    de propósito: obter_coordenadas (roteirizacao_dados.py) geocodifica
    pelo texto de 'address' via cache e ignora coordenada já presente no
    dict (mesmo achado documentado em rascunhos_rota.recalcular_km)."""
    return {"address": parada.get("endereco") or ""}


def _bairro_cidade(endereco: str) -> tuple[str | None, str | None]:
    """Best-effort a partir do endereço bruto da Vuupt ('Rua X, 123,
    Bairro, Cidade - UF, CEP, Brasil') -- só para exibição resumida ao
    motorista, nunca usado pra decisão de negócio. Remove o rabo ',
    CEP, Brasil' antes de separar (mesmas 2 regras de
    gerar_pdf_romaneios._endereco_entrega -- achado 22/08 em teste
    real: sem isso, 'Cidade - UF' nunca é o último segmento, e o
    CEP acaba virando "bairro" na tela)."""
    endereco = re.sub(r",?\s*Brasil\s*$", "", endereco or "", flags=re.IGNORECASE)
    endereco = re.sub(r",?\s*\d{5}-?\d{3}\s*$", "", endereco)
    partes = [p.strip() for p in endereco.split(",") if p.strip()]
    if not partes:
        return None, None
    match = _RE_UF_FINAL.match(partes[-1])
    cidade = match.group(1).strip() if match else partes[-1]
    bairro = partes[-2] if len(partes) >= 3 else None
    if bairro and re.fullmatch(r"\d+[A-Za-z]?", bairro):
        bairro = None  # segmento é só número -- endereço sem bairro separado.
    return bairro, cidade


def montar_resumo(paradas: list[dict], api_key: str | None = None) -> dict:
    """
    Resumo pronto pra publicar/exibir no marketplace:
        {
            "regiao": "ZONA SUL" | "Campinas, Sorocaba / VIAGEM" | "Zona não identificada",
            "cidades": [...],              # dedup, ordenado
            "bairros": [...],              # dedup, ordenado, best-effort
            "total_paradas": int,
            "paradas_por_nivel": {1: n, 2: n, 3: n, 4: n},
            "total_caixas": int,
            "peso_kg": float | None,       # None = nenhuma parada com peso conhecido ainda
            "peso_paradas_com_dado": int,
        }
    """
    from avisar_motoristas_rotas import _zona_da_rota
    from gerar_pdf_romaneios import calcular_peso_paradas

    sublote_fake = [_servico_fake(p) for p in paradas]
    regiao = _zona_da_rota(sublote_fake, api_key)

    cidades_por_chave: dict[str, str] = {}
    bairros_por_chave: dict[str, str] = {}
    for p in paradas:
        bairro, cidade = _bairro_cidade(p.get("endereco") or "")
        if cidade:
            cidades_por_chave.setdefault(cidade.upper(), cidade)
        if bairro:
            bairros_por_chave.setdefault(bairro.upper(), bairro)

    paradas_por_nivel = Counter(p.get("nivel_dificuldade") or 1 for p in paradas)
    total_caixas = sum(p.get("volume_caixas") or 0 for p in paradas)
    peso_kg, peso_paradas_com_dado, _total = calcular_peso_paradas(paradas)

    return {
        "regiao": regiao,
        "cidades": sorted(cidades_por_chave.values(), key=str.upper),
        "bairros": sorted(bairros_por_chave.values(), key=str.upper),
        "total_paradas": len(paradas),
        "paradas_por_nivel": dict(sorted(paradas_por_nivel.items())),
        "total_caixas": total_caixas,
        "peso_kg": round(peso_kg, 1) if peso_kg is not None else None,
        "peso_paradas_com_dado": peso_paradas_com_dado,
    }
