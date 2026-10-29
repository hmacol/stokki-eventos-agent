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
from regras import motoristas_banco
from regras.preferencias_motoristas import CatalogoMotoristas, _construir_motorista
from regras.tipo_veiculo import TIPOS_VEICULO
from regras.cadastro_motoristas import listar_agentes_vuupt_nao_cadastrados, cadastrar_motorista, editar_motorista, MotoristaNaoEncontrado

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
    rotulos_dias = [rotulo for _, rotulo in DIAS_SEMANA]
    codigos_dias = [codigo for codigo, _ in DIAS_SEMANA]
    logins = logins_do_app()

    def linha_da_tela(m, id_=None, bruto=None):
        bruto = bruto or {}
        return {
            "id": id_,
            "agent_id": m.agent_id, "nome": m.nome, "ativo": m.ativo,
            "dias_disponiveis": ", ".join(rotulos_dias[d] for d in sorted(m.dias_disponiveis) if 0 <= d < 7),
            "zonas_preferidas": m.zonas_preferidas, "tipo_veiculo": m.tipo_veiculo,
            "telefone": m.telefone, "email": m.email, "placa": m.placa,
            # login do app (08/10): cpf do login, se entrou e se vê o extrato
            "cpf_login": (logins.get(m.agent_id) or {}).get("cpf"),
            "ve_financeiro": bool((logins.get(m.agent_id) or {}).get("ve_financeiro", 1)),
            "ultimo_login_em": (logins.get(m.agent_id) or {}).get("ultimo_login_em"),
            # valores pro formulário de edição: texto cru da coluna (o JS só manda o que o usuário mudar)
            "form": {
                "id": id_, "agent_id": m.agent_id, "nome": m.nome, "aceita_viagens": m.aceita_viagens,
                "dias_disponiveis": [codigos_dias[d] for d in sorted(m.dias_disponiveis) if 0 <= d < 7],
                "max_rotas_dia": m.max_rotas_dia, "ativo": m.ativo, "zonas_preferidas": m.zonas_preferidas,
                "telefone": bruto.get("TELEFONE_MOTORISTA") or "", "email": bruto.get("EMAIL_MOTORISTA") or "",
                "placa": bruto.get("PLACA") or "", "cpf": bruto.get("CPF_MOTORISTA") or "",
                # coluna preenchida -> pré-seleciona o código canônico (aceita apelido); vazia -> "Nenhum"
                "tipo_veiculo": m.tipo_veiculo if (bruto.get("TIPO_VEICULO") or "").strip() else "",
                "vehicle_id": m.vehicle_id if m.vehicle_id is not None else "",
                "apenas_carga_seca": m.apenas_carga_seca, "sem_app": m.sem_app,
            },
        }

    registros = _registros_do_banco()
    if registros:
        lista = []
        for registro in registros:
            m = _construir_motorista(registro)
            if m:
                lista.append(linha_da_tela(m, registro["id"], registro))
    else:   # banco vazio (dev): catálogo antigo, sem botão Editar (sem id)
        config = _carregar_config()
        cfg_motoristas = config.get("motoristas", {})
        catalogo = CatalogoMotoristas.carregar(cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
        lista = [linha_da_tela(m) for m in catalogo.motoristas]
    motoristas = sorted(lista, key=lambda m: m["nome"])

    return {
        "motoristas": motoristas,
        "zonas_disponiveis": ZONAS_DISPONIVEIS,
        "dias_semana": DIAS_SEMANA,
        "tipos_veiculo": [{"codigo": t.codigo, "nome": t.nome} for t in TIPOS_VEICULO],
        "cadastros_pendentes": cadastros_pendentes(),
    }


def _registros_do_banco() -> list[dict]:
    """Linhas da tabela bd_motoristas (com id); vazio se a tabela está vazia ou o banco falha."""
    try:
        from nucleo import banco as nucleo_banco
        conn = nucleo_banco.conectar()
        try:
            return motoristas_banco.listar(conn)
        finally:
            conn.close()
    except Exception as e:   # pragma: no cover - defesa
        import logging
        logging.getLogger(__name__).warning(f"bd_motoristas indisponível: {e}")
        return []


def logins_do_app() -> dict[int, dict]:
    """agent_id -> login do app (cpf, ve_financeiro, ultimo_login_em). Vazio se o núcleo falhar."""
    try:
        from nucleo import banco as nucleo_banco, cadastro_motorista as cad
        conn = nucleo_banco.conectar()
        try:
            return cad.logins_por_agent_id(conn)
        finally:
            conn.close()
    except Exception as e:   # pragma: no cover - defesa
        import logging
        logging.getLogger(__name__).warning(f"Logins do app indisponíveis: {e}")
        return {}


def cadastros_pendentes() -> list[dict]:
    """Auto-cadastros vindos do app (nucleo/cadastro_motorista.py, 08/10)
    esperando o Hugo aprovar. Nunca derruba a tela: sem núcleo, lista vazia."""
    try:
        from nucleo import banco as nucleo_banco, cadastro_motorista as cad
        conn = nucleo_banco.conectar()
        try:
            return cad.listar_pendentes(conn)
        finally:
            conn.close()
    except Exception as e:   # pragma: no cover - defesa
        import logging
        logging.getLogger(__name__).warning(f"Cadastros pendentes indisponíveis: {e}")
        return []
