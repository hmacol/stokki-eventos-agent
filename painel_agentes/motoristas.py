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
            "agent_id": m.agent_id, "nome": m.nome, "ativo": m.ativo, "posicao": m.posicao,
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
                "posicao": m.posicao,
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
    padrao, total_pendentes = _padrao_por_linha()
    for m in motoristas:
        m.update(padrao.get(m["id"]) or {
            "principal_id": None, "pendencias": {}, "tem_pendencia": False, "estado_link": None, "link_data": None,
            "rotas_30d": None, "grupo_nome": None, "tem_login": bool(m["cpf_login"]), "tem_ficha": False})

    return {
        "motoristas": motoristas,
        "total_pendentes": total_pendentes,
        "zonas_disponiveis": ZONAS_DISPONIVEIS,
        "dias_semana": DIAS_SEMANA,
        "tipos_veiculo": [{"codigo": t.codigo, "nome": t.nome} for t in TIPOS_VEICULO],
        "cadastros_pendentes": cadastros_pendentes(),
        "cadastros_link": cadastros_link(),
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


def url_app_motorista(config: dict) -> str:
    """Endereço público do app dos motoristas (onde fica /atualizar/<token>). Não há
    chave própria: sai do portal_cliente.chamados.url_painel trocando o /painel do fim
    por /motorista; fora desse formato, o endereço de produção."""
    url = str((((config or {}).get("portal_cliente") or {}).get("chamados") or {}).get("url_painel")
              or "https://app.freshhub.com.br/painel").rstrip("/")
    if url.endswith("/painel"):
        return url[:-len("/painel")] + "/motorista"
    return "https://app.freshhub.com.br/motorista"


def _padrao_por_linha() -> tuple[dict, int]:
    """id da linha -> pendências da pessoa, estado do link, rotas 30 d, grupo, login e
    ficha; mais o total de PESSOAS com pendência. Liga antes as fichas antigas sem
    vínculo (idempotente). Falha: ({}, 0) e a tela segue sem os selos."""
    try:
        from nucleo import (atualizacao_motorista as atu, auth_motorista as auth, banco as nucleo_banco,
                            cadastro_motorista as cad, grupos_motoristas as grupos)
        from regras.preferencias_motoristas import _parse_bool
        conn = nucleo_banco.conectar()
        try:
            atu.vincular_fichas(conn)
            rotas = grupos.rotas_30d(conn)
            vinculos = grupos._vinculos(conn)   # privado do backend (fora desta tarefa); só leitura
            com_ficha = {r[0] for r in conn.execute(
                "SELECT DISTINCT bd_motorista_id FROM motoristas_cadastros WHERE status = ? "
                "AND bd_motorista_id IS NOT NULL", (cad.STATUS_APROVADO,))}
            por_linha, total = {}, 0
            for p in atu.pessoas(conn):
                pid = p["principal_id"]
                pend = atu.pendencias(conn, p) if p["ativo"] else {}
                tem = bool(pend.get("documentos") or pend.get("campos") or pend.get("veiculo_diferente"))
                total += tem
                estado = atu.estado_link(conn, pid)
                aberto = atu.link_em_aberto(conn, pid) if estado == "enviado" else None
                quando = aberto and (aberto.get("enviado_em") or aberto.get("criado_em")) or ""
                rotas_pessoa = sum(rotas.get(a, 0) for a in p["agent_ids"])   # por pessoa, igual em toda linha
                for linha in p["linhas"]:
                    ativa = _parse_bool(linha.get("ATIVO"))
                    cpf = auth.normalizar_cpf(linha.get("CPF_MOTORISTA"))
                    por_linha[linha["id"]] = {
                        "principal_id": pid,
                        "pendencias": pend if ativa else {},
                        "tem_pendencia": tem and ativa,
                        "estado_link": estado,
                        "link_data": f"{quando[8:10]}/{quando[5:7]}" if quando else None,
                        "rotas_30d": rotas_pessoa,
                        "grupo_nome": (vinculos.get(pid) or {}).get("grupo_nome"),
                        "tem_login": bool(cpf and auth.buscar_motorista(conn, cpf)),
                        "tem_ficha": any(i in com_ficha for i in p["ids"]),
                    }
            return por_linha, total
        finally:
            conn.close()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Pendências dos motoristas indisponíveis: {e}")
        return {}, 0


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


def cadastros_link() -> list[dict]:
    """Cadastros feitos pelo link (nucleo/cadastro_motorista_link.py) enviados nos
    últimos 30 dias ou ainda não concluídos, mais novos primeiro. Nunca derruba a tela."""
    try:
        from datetime import datetime, timedelta
        from nucleo import banco as nucleo_banco, cadastro_motorista as cad
        corte = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn = nucleo_banco.conectar()
        try:
            rows = conn.execute(
                "SELECT * FROM motoristas_cadastros WHERE origem = ? AND enviado_em IS NOT NULL "
                "AND (enviado_em >= ? OR concluido_em IS NULL) ORDER BY id DESC", (cad.ORIGEM_LINK, corte)).fetchall()
            return [cad._montar(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:   # pragma: no cover - defesa
        import logging
        logging.getLogger(__name__).warning(f"Cadastros pelo link indisponíveis: {e}")
        return []
