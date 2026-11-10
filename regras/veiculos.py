# -*- coding: utf-8 -*-
"""Regras de veiculo do motorista: motorista de verdade sai sempre com carro.

Veiculo generico da Vuupt (placa comeca com ".": ".Seco", "..Carro_Grande")
nao conta como carro. Agentes virtuais (Lalamove, Retirada) nunca "faltam carro".
"""
import threading
import time

AGENTES_SEM_CARRO = frozenset({50258, 50259})  # Lalamove, Retirada
TTL_CACHE_SEG = 600

_cache = {"em": None, "dados": None}
_trava = threading.Lock()


def eh_generico(placa):
    return (placa or "").strip().startswith(".")


def falta_carro(agent_id, vehicle_id, placa_do_veiculo):
    if not agent_id or agent_id in AGENTES_SEM_CARRO:
        return False
    if not vehicle_id:
        return True
    return eh_generico(placa_do_veiculo)


def texto_sem_carro(nome):
    return f"Motorista {nome} sem carro cadastrado: escolha o carro no card ou cadastre o veículo dele."


def placas_por_id(veiculos_vuupt):
    return {v.get("id"): v.get("license_plate") for v in (veiculos_vuupt or [])}


def rascunho_sem_carro(rascunho, placas):
    """placas: {vehicle_id: placa}, ou None se a lista nao carregou (ai so vale vehicle_id vazio)."""
    placa = (placas or {}).get(rascunho.get("vehicle_id"))
    return falta_carro(rascunho.get("agent_id"), rascunho.get("vehicle_id"), placa)


def verificar_carro(rascunho, placas):
    """Texto de erro se o rascunho sai sem carro; None se esta ok."""
    if not rascunho_sem_carro(rascunho, placas):
        return None
    return texto_sem_carro(rascunho.get("motorista_nome") or rascunho.get("agent_id"))


def anotar_falta_carro(rascunhos, placas):
    for r in rascunhos:
        r["falta_carro"] = rascunho_sem_carro(r, placas)


def listar_para_seletor(veiculos_vuupt, motoristas, data=None):
    donos = {m.get("vehicle_id"): m.get("nome") for m in motoristas if m.get("vehicle_id")}
    itens = [
        {"vehicle_id": v["id"], "placa": v.get("license_plate"), "dono": donos.get(v["id"])}
        for v in veiculos_vuupt
        if not v.get("deleted_at") and not eh_generico(v.get("license_plate"))
    ]
    itens.sort(key=lambda i: (i["dono"] is None, i["placa"] or ""))
    if data is not None:
        # mesma regra de placa/dia da trava da alocacao (roteirizacao/rodizio_sp)
        from rodizio_sp import placa_restrita_no_dia
        for i in itens:
            i["rodizio_no_dia"] = placa_restrita_no_dia(i["placa"], data.weekday())
    return itens


def listar_veiculos_cacheado(carregar, agora=time.monotonic):
    """Lista de veiculos da Vuupt com cache em memoria de 10 min (compartilhado
    pelo seletor, pela tela e pela trava de envio). carregar() faz a chamada."""
    with _trava:
        t = agora()
        if _cache["dados"] is not None and t - _cache["em"] < TTL_CACHE_SEG:
            return _cache["dados"]
        dados = carregar()
        _cache["dados"], _cache["em"] = dados, t
        return dados


def limpar_cache():
    with _trava:
        _cache["dados"], _cache["em"] = None, None
