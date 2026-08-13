# -*- coding: utf-8 -*-
"""
planejamento_rotas.py

Monta os dados da tela de Planejamento de Rotas (editor de rascunhos,
Fase 2 do plano do Hugo, 12/08) -- equivalente ao papel de mapa_rotas.
py::buscar_rotas_para_mapa, mas lendo de rascunhos_rota (rascunhos
locais, ainda não enviados à VUUPT) em vez de rotas já criadas lá.

Monta também o POOL de pedidos not_assigned que não estão em nenhum
rascunho do lote ativo -- não existe tabela própria pra isso (ver
rascunhos_rota.py): um not_assigned que não aparece em nenhuma
rascunhos_parada do lote ativo da data JÁ É o pool, comparado ao vivo
contra a VUUPT pra nunca ficar dessincronizado.

As travas de agrupamento (18 paradas / 100 caixas / 20km / nível 3-4,
mesmas de roteirizacao_dados.py::dividir_em_sublotes) são reavaliadas
aqui sobre o estado ATUAL de cada rascunho (que pode já ter sido
editado manualmente) só pra gerar um aviso visual -- a edição manual
NUNCA é bloqueada por causa delas (decisão do Hugo, 12/08).
"""
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import yaml

from vuupt_client import VuuptClient
from roteirizacao_dados import elegivel_para_data, extrair_volume_caixas, extrair_nivel_dificuldade, _distancia_km
from regioes_dia_fixo import DIAS_NOMES, extrair_cidade, regiao_da_cidade, regra_dia_fixo_do_servico
from regras.preferencias_motoristas import CatalogoMotoristas
from mapa_util import carregar_remetentes_por_sender_id

import rascunhos_rota

logger = logging.getLogger(__name__)

_PADRAO_SUFIXO_REENTREGA = re.compile(r"-R\d+$")


def _codigo_base(codigo: str) -> str:
    """'PS-36327-R1' -> 'PS-36327' -- reentrega é o MESMO pedido
    original (VUUPT só duplica o serviço, ver insucesso_entrega/
    expedir_pedidos.py::duplicar_servico_por_insucesso); documentos e
    o pedido na Stokki existem sob o código BASE, sem sufixo. Mesma
    normalização de roteirizacao/gerar_pdf_romaneios.py::_codigo_base
    (achado 12/08: sem ela, reentrega nunca casa com NF/boleto nem
    encontra o pedido na Stokki)."""
    return _PADRAO_SUFIXO_REENTREGA.sub("", codigo or "")


TAMANHO_MAXIMO_ROTA = 18
NIVEL_3_TAMANHO_MAXIMO_ROTA = 4
VOLUME_MAXIMO_ROTA = 100
DISTANCIA_MAXIMA_ROTA_KM = 20
# Pedido "grande": acima disso vira alerta visual nos cards e no resumo
# do futuro (pedido do Hugo, 12/08) -- um pedido desses sozinho já
# ocupa boa parte do VOLUME_MAXIMO_ROTA de uma rota e merece atenção
# na hora de montar o dia (veículo/rota própria).
LIMITE_ALERTA_CAIXAS = 70


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _badges_trava(rascunho: dict) -> list[str]:
    """Avisos visuais (não bloqueiam) quando o rascunho, do jeito que
    está AGORA, estouraria alguma trava de roteirizacao_dados.py::
    dividir_em_sublotes -- mesmos limites, só que como aviso em vez de
    impedimento (a edição manual pode ter motivo legítimo pra furar)."""
    paradas = rascunho["paradas"]
    badges = []
    if len(paradas) > TAMANHO_MAXIMO_ROTA:
        badges.append(f"{len(paradas)} paradas (máx {TAMANHO_MAXIMO_ROTA})")

    caixas = sum(p["volume_caixas"] or 1 for p in paradas)
    if caixas > VOLUME_MAXIMO_ROTA:
        badges.append(f"{caixas} caixa(s) (máx {VOLUME_MAXIMO_ROTA})")

    niveis = [p["nivel_dificuldade"] or 1 for p in paradas]
    if len(paradas) > 1 and any(n >= 4 for n in niveis):
        badges.append("entrega nível 4 dividindo rota com outras")
    elif any(n == 3 for n in niveis) and len(paradas) > NIVEL_3_TAMANHO_MAXIMO_ROTA:
        badges.append(f"entrega nível 3 com mais de {NIVEL_3_TAMANHO_MAXIMO_ROTA} paradas na rota")

    if rascunho.get("tipo_rota") != "VIAGEM":
        coords = [(p["latitude"], p["longitude"]) for p in paradas if p["latitude"] and p["longitude"]]
        for i in range(len(coords)):
            for j in range(i + 1, len(coords)):
                dist = _distancia_km(*coords[i], *coords[j])
                if dist > DISTANCIA_MAXIMA_ROTA_KM:
                    badges.append(f"paradas a {dist:.0f}km entre si (máx {DISTANCIA_MAXIMA_ROTA_KM}km)")
                    break
            else:
                continue
            break

    return badges


def _data_agendada(servico: dict) -> date | None:
    """Data do scheduled_start do serviço, ou None se vazio/malformado
    (malformado = tratado como sem agendamento, o mesmo critério de
    roteirizacao_dados.py::elegivel_para_data)."""
    scheduled_start = servico.get("scheduled_start")
    if not scheduled_start:
        return None
    try:
        return datetime.fromisoformat(scheduled_start).date()
    except (ValueError, TypeError):
        return None


def _servico_para_pool(servico: dict, remetentes_por_id: dict[int, str]) -> dict:
    lat, lng = servico.get("latitude"), servico.get("longitude")
    agendado = _data_agendada(servico)
    return {
        "agendado_para": agendado.isoformat() if agendado else None,
        "service_id": servico["id"],
        "codigo": servico.get("code", ""),
        "titulo": servico.get("title", ""),
        "endereco": servico.get("address", ""),
        "latitude": float(lat) if lat not in (None, "") else None,
        "longitude": float(lng) if lng not in (None, "") else None,
        "sender_id": servico.get("sender_id"),
        "remetente_nome": remetentes_por_id.get(servico.get("sender_id"), "Remetente não identificado"),
        "destinatario_nome": (servico.get("customer") or {}).get("name") or "",
        "nivel_dificuldade": extrair_nivel_dificuldade(servico),
        "volume_caixas": extrair_volume_caixas(servico),
    }


def _regiao_do_servico(servico: dict) -> str:
    """
    Rótulo de região pro resumo do futuro. Prioridade igual à das regras
    de dia fixo: galpão/operador logístico identificado pelo endereço
    (TAFF, Transfrios...), depois a REGIÃO da cidade (regioes_dia_fixo.
    REGIOES -- "Vale do Paraíba", "ABCD"...), depois a própria cidade
    (Grande SP sem dia fixo), e por fim um balde genérico quando nem a
    cidade dá pra extrair do endereço.
    """
    regra = regra_dia_fixo_do_servico(servico)
    if regra and regra["origem"] == "endereco":
        return regra["nome"]
    cidade = extrair_cidade(servico)
    if cidade:
        return regiao_da_cidade(cidade) or cidade.title()
    return "Sem região identificada"


def _resumo_agendados_futuros(servicos_brutos: list[dict], data_alvo: date) -> list[dict]:
    """
    Agrega os not_assigned agendados pra DEPOIS de data_alvo num
    "resumo do futuro" (pedido do Hugo, 12/08: "mostre também os
    pedidos que estão agendados, quantidade de pedidos e volumes para
    cada região"): um bloco por dia, em ordem cronológica, cada um com
    as regiões e seus totais de pedidos e caixas. Cada região traz
    também "grandes": os pedidos acima de LIMITE_ALERTA_CAIXAS, que
    viram alerta visual no card do dia.

    scheduled_start malformado é ignorado aqui de propósito -- pedido
    assim é tratado como SEM agendamento por elegivel_para_data, ou
    seja, já aparece no pool do dia; contar de novo no futuro duplicaria.
    """
    por_dia: dict[date, dict[str, dict]] = {}
    for s in servicos_brutos:
        data_agendada = _data_agendada(s)
        if not data_agendada or data_agendada <= data_alvo:
            continue  # sem agendamento (ou malformado) ou já elegível -- assunto do pool

        regioes_do_dia = por_dia.setdefault(data_agendada, {})
        reg = regioes_do_dia.setdefault(_regiao_do_servico(s), {"pedidos": 0, "caixas": 0, "codigos": [], "grandes": []})
        caixas = extrair_volume_caixas(s)
        reg["pedidos"] += 1
        reg["caixas"] += caixas
        reg["codigos"].append(s.get("code", ""))
        if caixas > LIMITE_ALERTA_CAIXAS:
            reg["grandes"].append(f"{s.get('code', '')} ({caixas} cx)")

    resumo = []
    for data_agendada in sorted(por_dia):
        regioes = [
            {"regiao": nome, **info}
            for nome, info in sorted(por_dia[data_agendada].items(),
                                     key=lambda item: (-item[1]["pedidos"], item[0]))
        ]
        resumo.append({
            "data_iso": data_agendada.isoformat(),
            "data_fmt": data_agendada.strftime("%d/%m"),
            "dia_semana": DIAS_NOMES[data_agendada.weekday()],
            "total_pedidos": sum(r["pedidos"] for r in regioes),
            "total_caixas": sum(r["caixas"] for r in regioes),
            "regioes": regioes,
        })
    return resumo


def buscar_pool_e_agendados(data_alvo: date, config: dict | None = None) -> dict:
    """
    Busca ao vivo na VUUPT os pedidos not_assigned e separa, com UMA
    chamada só à API, em:

      - "pool": elegíveis pra essa data que ainda não estão em nenhum
        rascunho ATIVO (a coluna arrastável da tela) -- extraída de
        buscar_dados_planejamento() pra ser reaproveitada pelo botão
        "Atualizar" (Hugo, 12/08: verificar pedido novo chegando na
        VUUPT sem recarregar a página, perdendo a edição em andamento);
      - "resumo_futuro": agendados pra depois dessa data, agregados por
        dia e região (_resumo_agendados_futuros). Aqui NÃO se desconta
        quem está em rascunho: rascunho ativo é sempre da data em
        edição, e o resumo é justamente a demanda que ainda vai chegar;
      - "agendamentos_por_service_id": {service_id: data ISO} de TODO
        not_assigned com agendamento válido -- usado por
        buscar_dados_planejamento pra mostrar o agendamento também nas
        paradas já em rascunho (que continuam not_assigned na VUUPT até
        o envio), sem precisar de coluna nova em rascunhos_parada.
    """
    config = config or _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")

    rascunhos_ativos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    ids_em_rascunho = {p["service_id"] for r in rascunhos_ativos for p in r["paradas"]}

    remetentes_por_id = carregar_remetentes_por_sender_id()
    vuupt = VuuptClient(token)
    filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
    servicos_brutos = vuupt.listar_servicos(filtro, per_page=100, include=["customer"])
    pool = [
        _servico_para_pool(s, remetentes_por_id)
        for s in servicos_brutos
        if s["id"] not in ids_em_rascunho and elegivel_para_data(s, data_alvo)
    ]
    pool.sort(key=lambda p: p["codigo"])

    agendamentos_por_service_id = {}
    for s in servicos_brutos:
        agendado = _data_agendada(s)
        if agendado:
            agendamentos_por_service_id[s["id"]] = agendado.isoformat()

    return {
        "pool": pool,
        "resumo_futuro": _resumo_agendados_futuros(servicos_brutos, data_alvo),
        "agendamentos_por_service_id": agendamentos_por_service_id,
    }


def buscar_dados_planejamento(data_alvo: date | None = None) -> dict:
    """
    Retorna {"data_alvo", "rascunhos": [...], "pool": [...],
    "resumo_futuro": [...], "base", "google_maps_key",
    "motoristas": [...]} pra tela de planejamento.

    Cada rascunho vem com "badges" (avisos de trava estourada) já
    calculados. O pool é o not_assigned elegível pra essa data que
    ainda não está em nenhum rascunho do lote ativo; resumo_futuro é a
    demanda agendada pra depois dela, por dia e região.
    """
    data_alvo = data_alvo or date.today()
    config = _carregar_config()
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    from geocodificacao import geocodificar
    ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"
    coords_base = geocodificar(ENDERECO_BASE, gmaps_key)

    rascunhos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    for r in rascunhos:
        r["badges"] = _badges_trava(r)

    pool_e_agendados = buscar_pool_e_agendados(data_alvo, config)

    # agendamento das paradas já em rascunho vem da MESMA busca do pool
    # (elas continuam not_assigned na VUUPT até o envio) -- pedido do
    # Hugo, 12/08: mostrar o agendamento também nos cards das rotas
    agendamentos = pool_e_agendados["agendamentos_por_service_id"]
    for r in rascunhos:
        for p in r["paradas"]:
            p["agendado_para"] = agendamentos.get(p["service_id"])

    cfg_motoristas = config.get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
    motoristas = [
        {"agent_id": m.agent_id, "vehicle_id": m.vehicle_id, "nome": m.nome}
        for m in catalogo.motoristas
    ]

    return {
        "data_alvo": data_alvo.strftime("%d/%m/%Y"),
        "data_alvo_iso": data_alvo.isoformat(),
        "rascunhos": rascunhos,
        "pool": pool_e_agendados["pool"],
        "resumo_futuro": pool_e_agendados["resumo_futuro"],
        "base": {"lat": coords_base[0], "lng": coords_base[1]} if coords_base else None,
        "google_maps_key": gmaps_key,
        "motoristas": motoristas,
        "limite_alerta_caixas": LIMITE_ALERTA_CAIXAS,
    }


PASTA_ROMANEIOS_RASCUNHO = _RAIZ / "painel_agentes" / "dados" / "romaneios_rascunho"


def gerar_romaneio_pdf(rascunho_id: int) -> Path:
    """
    Gera o PDF de romaneio (capa com status de NF/boleto por pedido +
    NFs e boletos emendados na ordem de visita + canhoteira, quando
    aplicável) do rascunho -- botão "Imprimir rota" da tela (Hugo,
    12/08: "resgatar aquele fluxo de documentos que criamos").

    Reaproveita 100% o motor de roteirizacao/gerar_pdf_romaneios.py
    (mesmo usado pros romaneios das rotas já criadas na VUUPT, job das
    04h) -- só que aplicado direto sobre o RASCUNHO local, o que
    inclusive é uma vantagem: dá pra conferir a papelada (que pedido
    tem NF, qual falta boleto) ANTES de confirmar o envio, não só
    depois.

    Documentos são casados por código do pedido (PS-XXXXX) na tabela
    documentos_processados -- pedido sem NF/boleto localizado aparece
    como pendência na capa (X vermelho), não impede a geração do PDF.
    """
    sys.path.insert(0, str(_RAIZ / "roteirizacao"))
    import gerar_pdf_romaneios as gpr

    rascunho = rascunhos_rota.buscar_rascunho(rascunho_id)
    if not rascunho:
        raise ValueError(f"Rascunho {rascunho_id} não encontrado.")
    if not rascunho["paradas"]:
        raise ValueError("Rascunho sem paradas -- nada pra imprimir.")

    data_alvo = date.fromisoformat(rascunho["data_alvo"])
    servicos = [
        {"code": p["codigo"], "title": p["titulo"], "sender_id": p["sender_id"]}
        for p in rascunho["paradas"]
    ]
    codigos = {_codigo_base(s["code"]) for s in servicos}

    docs_por_pedido, _em_revisao = gpr.carregar_documentos_por_pedido(codigos)
    embarcadores = gpr.carregar_embarcadores()
    nome_motorista = rascunho.get("motorista_nome") or "(sem motorista)"
    rota_fake = {"name": rascunho["nome"], "id": rascunho["id"]}

    PASTA_ROMANEIOS_RASCUNHO.mkdir(parents=True, exist_ok=True)
    caminho_saida = PASTA_ROMANEIOS_RASCUNHO / f"rascunho_{rascunho_id}.pdf"

    gpr.montar_pdf_rota(rota_fake, servicos, docs_por_pedido, embarcadores,
                        nome_motorista, data_alvo, caminho_saida)
    return caminho_saida


def carregar_documentos_do_rascunho(rascunho_id: int) -> dict:
    """
    Roda o fluxo de documentos (documentos_pedido/processar_documentos.
    py) escopado só aos pedidos DESSE rascunho -- pedido do Hugo, 12/08:
    o job agendado só roda às 18h, tarde demais se o romaneio precisa
    ser impresso antes disso; buscar na hora garante NF/boleto
    atualizados antes de montar o PDF (gerar_romaneio_pdf).

    Só a etapa da STOKKI é escopada por pedido (--pedidos); as etapas
    de e-mail (busca ampla + embarcadores conhecidos) não têm como ser
    restritas por pedido e continuam rodando normais -- ainda assim
    relativamente rápidas (não abrem navegador por pedido). Notificação
    de execução é suprimida aqui (rodar a cada clique de "Imprimir
    rota" ia encher a caixa de entrada) -- fica só no job agendado.

    Retorna os contadores de documentos_pedido.processar_documentos.main
    (ENVIADO/REVISAO_MANUAL/JA_PROCESSADO/ERRO).
    """
    sys.path.insert(0, str(_RAIZ / "documentos_pedido"))
    import processar_documentos as pdoc

    rascunho = rascunhos_rota.buscar_rascunho(rascunho_id)
    if not rascunho:
        raise ValueError(f"Rascunho {rascunho_id} não encontrado.")
    codigos = sorted({_codigo_base(p["codigo"]) for p in rascunho["paradas"] if p["codigo"]})
    if not codigos:
        return {"JA_PROCESSADO": 0, "ENVIADO": 0, "REVISAO_MANUAL": 0, "ERRO": 0}

    return pdoc.main(modo_teste=False, pedidos_stokki=codigos, notificar=False)
