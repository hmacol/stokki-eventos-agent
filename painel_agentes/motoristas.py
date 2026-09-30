# -*- coding: utf-8 -*-
"""
motoristas.py

Dados da tela "Motoristas" do painel (pedido do Hugo, 16/08) --
listagem de quem já está em dados/BD_MOTORISTAS.xlsx e cadastro de
motorista novo (regras/cadastro_motoristas.py faz a escrita; este
módulo só monta o que a página/API precisam).
"""
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import yaml

from zonas_sp import (
    ZONA_NORTE, ZONA_SUL, ZONA_LESTE, ZONA_OESTE, CENTRO, GUARULHOS,
    ABCD, OSASCO_BARUERI_ALPHAVILLE, COTIA_EMBU_TABOAO,
)
from regras.preferencias_motoristas import CatalogoMotoristas
from regras.tipo_veiculo import TIPOS_VEICULO
from regras.cadastro_motoristas import listar_agentes_vuupt_nao_cadastrados, cadastrar_motorista

ZONAS_DISPONIVEIS = [
    ZONA_NORTE, ZONA_SUL, ZONA_LESTE, ZONA_OESTE, CENTRO,
    GUARULHOS, ABCD, OSASCO_BARUERI_ALPHAVILLE, COTIA_EMBU_TABOAO,
]

DIAS_SEMANA = [
    ("SEGUNDA", "Seg"), ("TERCA", "Ter"), ("QUARTA", "Qua"), ("QUINTA", "Qui"),
    ("SEXTA", "Sex"), ("SABADO", "Sáb"), ("DOMINGO", "Dom"),
]


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def dados_pagina_motoristas() -> dict:
    """Lista pra tabela da tela + as opções (zonas/dias/tipos de
    veículo) pro formulário de cadastro."""
    config = _carregar_config()
    cfg_motoristas = config.get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))

    rotulos_dias = [rotulo for _, rotulo in DIAS_SEMANA]
    motoristas = sorted((
        {
            "agent_id": m.agent_id, "nome": m.nome, "ativo": m.ativo,
            "dias_disponiveis": ", ".join(rotulos_dias[d] for d in sorted(m.dias_disponiveis) if 0 <= d < 7),
            "zonas_preferidas": m.zonas_preferidas, "tipo_veiculo": m.tipo_veiculo,
            "telefone": m.telefone, "email": m.email, "placa": m.placa,
        }
        for m in catalogo.motoristas
    ), key=lambda m: m["nome"])

    return {
        "motoristas": motoristas,
        "zonas_disponiveis": ZONAS_DISPONIVEIS,
        "dias_semana": DIAS_SEMANA,
        "tipos_veiculo": [{"codigo": t.codigo, "nome": t.nome} for t in TIPOS_VEICULO],
    }
