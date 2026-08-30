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
import json
import logging
import re
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import yaml

from vuupt_client import VuuptClient, VuuptAPIError, _converter_data_para_iso
from roteirizacao_dados import (
    extrair_volume_caixas, _distancia_km, estimar_tempo_rota, definir_coords_base,
    ROTA_TEMPO_MAXIMO_HORAS, TEMPO_NIVEL3_HORAS, TEMPO_PARADA_NORMAL_HORAS,
)
from regioes_dia_fixo import DIAS_NOMES, extrair_cidade, regiao_da_cidade, regra_dia_fixo_do_servico
from regras.complexidade_entrega import (
    carregar_niveis, carregar_horarios, carregar_ajustes_manuais,
    definir_ajuste_manual, nivel_efetivo, horario_efetivo, NIVEIS_VALIDOS,
)
from regras.preferencias_motoristas import CatalogoMotoristas
from regras.confirmacao_rotas import listar_do_dia as listar_confirmacoes_do_dia
from regras.disponibilidade_motoristas import (
    carregar_ajustes_dia, definir_disponibilidade_dia, definir_disponibilidade_periodo, limpar_ajuste,
)
from regras.tipo_carga_embarcador import carregar_tipos_carga_por_sender
from regras.tipo_veiculo import tipo_por_codigo, TIPOS_VEICULO
from retiradas.regras_retirada import (PREFIXO_TITULO, STATUSES_ABERTOS, config_retiradas,
                                       data_prevista_do_servico, eh_servico_retirada)
# regras.ofertas_rota / regras.resumo_oferta (marketplace de rotas, Hugo
# 22/08) NÃO são importados aqui em cima de propósito -- ainda não foram
# deployados (falta config Chatwoot/template Meta, ver memória do
# projeto), então um import de módulo aqui quebraria a tela inteira em
# quem não tiver esses arquivos. Importados sob demanda, só nas 3 funções
# que realmente usam o marketplace (buscar_dados_planejamento,
# publicar_oferta_rascunho, despublicar_oferta_rascunho).
# listar_motoristas_elegiveis (marketplace, Hugo 22/08) importado sob
# demanda dentro de publicar_oferta_rascunho -- mesmo motivo do
# ofertas_rota/resumo_oferta acima, ainda não deployado.
from alocacao_motoristas import selecionar_motorista_equitativo
from mapa_util import carregar_remetentes_por_sender_id
from executor import buscar_ultima_execucao, progresso_execucao

import rascunhos_rota

logger = logging.getLogger(__name__)

# Agentes acionáveis pela barra de botões da tela de planejamento --
# pedido do Hugo, 14/08: "Executar tudo, Importação (com filtro de
# embarcador/pedido), Criar Rotas Diárias (Rascunho), Incrementar
# Rotas, Gerar PDFs de Romaneio" direto na tela, sem precisar ir no
# painel de agentes. Ordem = ordem operacional (e também a ordem em
# que "Executar tudo" roda cada um, respeitando incluir_executar_tudo
# -- ver AGENTES_PLANEJAMENTO_EXECUTAR_TUDO_IDS em painel_agentes.py).
# Mesmo padrão de torre_controle.py::ETAPAS_PIPELINE/
# montar_etapas_pipeline, só que restrito aos agentes relevantes pra
# essa tela (não inclui, por exemplo, Relatório, que é só da torre).
# Processar Documentos entra ANTES de Gerar Romaneios (pedido do
# Hugo, 20/08): gerar_pdf_romaneios.py lê NF/boleto do índice/pastas
# locais que processar_documentos.py alimenta -- sem rodar antes, o
# romaneio sai sem a papelada.
# Estação de Impressão e Expedição entraram 20/08 (pedido do Hugo):
# Impressão vai no INÍCIO (move pedidos faturados de 'Em espera' pra
# 'Aguardando Transportador' na Stokki -- pré-requisito pra Importação
# conseguir puxar esses pedidos, mesma ordem do executar_tudo.py) e
# participa do "Executar tudo"; Expedição vai no FIM (atua sobre
# pedidos já ENTREGUES, não faz parte do fluxo de criar rota pro dia
# seguinte) e fica FORA do "Executar tudo" -- roda pela tarefa
# agendada própria, foi tirada de propósito do executar_tudo.py em
# 06/08 e essa tela não deve reintroduzir isso sem o Hugo pedir.
ETAPAS_AGENTES_PLANEJAMENTO = [
    {"agente_id": "somente_impressao",             "titulo": "Estação de Impressão",
     "detalhe": "Em espera → Aguardando Transportador (Stokki)"},
    {"agente_id": "somente_importacao",            "titulo": "Importação",
     "detalhe": "Stokki → VUUPT (todos, ou filtrado por pedido/embarcador)"},
    {"agente_id": "criar_rotas_diarias_rascunho",  "titulo": "Criar Rotas Diárias (Rascunho)",
     "detalhe": "gera rascunhos locais pra revisão nessa tela"},
    {"agente_id": "incrementar_rotas",             "titulo": "Incrementar Rotas",
     "detalhe": "aloca pedidos novos nas rotas do dia já criadas"},
    {"agente_id": "processar_documentos",          "titulo": "Processar Documentos",
     "detalhe": "busca NF/Boleto por e-mail e na Stokki, casa com o pedido e envia pro GCS"},
    {"agente_id": "gerar_romaneios",               "titulo": "Gerar PDFs de Romaneio",
     "detalhe": "1 PDF por rota do dia, na ordem de visita"},
    {"agente_id": "somente_expedicao",             "titulo": "Expedição",
     "detalhe": "entregues → Stokki (todos, ou só pedidos selecionados)", "incluir_executar_tudo": False},
]


def montar_etapas_agentes_planejamento() -> list[dict]:
    """Última execução de cada agente da barra de planejamento (leitura
    barata, só o SQLite do painel) -- mesmo formato de
    torre_controle.montar_etapas_pipeline."""
    etapas = []
    for etapa in ETAPAS_AGENTES_PLANEJAMENTO:
        ultima = buscar_ultima_execucao(etapa["agente_id"])
        rodando = bool(ultima and ultima["status"] == "RODANDO")
        na_fila = bool(ultima and ultima["status"] == "NA_FILA")
        progresso = progresso_execucao(ultima["id"]) if rodando else None
        etapas.append({
            **etapa,
            "status": ultima["status"] if ultima else None,
            "quando": (ultima.get("finalizado_em") or ultima.get("iniciado_em")) if ultima else None,
            "execucao_id": ultima["id"] if ultima else None,
            "rodando": rodando,
            "na_fila": na_fila,
            "percentual": (progresso or {}).get("percentual"),
            "eta_segundos": (progresso or {}).get("eta_segundos"),
        })
    return etapas

_DB_PATH = _RAIZ / "dados" / "dados.db"

_PADRAO_CODIGO_BASE = re.compile(r"PS-?\d{4,6}", re.IGNORECASE)


def _codigo_base(codigo: str) -> str:
    """'#PS-36327-R1' -> 'PS-36327' -- reentrega é o MESMO pedido
    original (VUUPT só duplica o serviço, ver insucesso_entrega/
    expedir_pedidos.py::duplicar_servico_por_insucesso); documentos e
    o pedido na Stokki existem sob o código BASE, sem sufixo e sem '#'
    (matcher.py grava codigo_pedido sempre sem '#'). Mesma normalização
    de roteirizacao/gerar_pdf_romaneios.py::_codigo_base (achado 12/08:
    sem ela, reentrega nunca casa com NF/boleto nem encontra o pedido
    na Stokki).

    Extrai o PREFIXO 'PS-NNNNN' em vez de remover sufixo do FIM da
    string (achado 20/08): reentrega de reentrega empilha sufixo --
    'PS-36741-R1-R1' -- e uma regex ancorada em '$' só tira o ÚLTIMO
    '-R\\d+', devolvendo 'PS-36741-R1' em vez do código base. Casar
    pelo prefixo é imune a qualquer sufixo/combinação que apareça
    depois (-R1, -C1, -R1-R1, -R2-C1...)."""
    m = _PADRAO_CODIGO_BASE.match((codigo or "").lstrip("#").strip())
    return m.group(0) if m else (codigo or "").lstrip("#")


def _codigos_base_lista(codigo: str) -> list[str]:
    """'code' da VUUPT pode agrupar mais de um pedido combinado por
    vírgula (achado 20/08, ver roteirizacao/gerar_pdf_romaneios.py::
    _codigos_base_lista) -- quebra em códigos individuais antes de
    normalizar, senão a busca de documentos nunca casa nada pro grupo."""
    return [_codigo_base(c.strip()) for c in (codigo or "").split(",") if c.strip()]


TAMANHO_MAXIMO_ROTA = 16  # Voltou de 14 para 16 (pedido do Hugo, 22/08) -- mesmo teto de criar_rotas_diarias.py (constantes separadas, sem import entre os dois módulos)
NIVEL_3_TAMANHO_MAXIMO_ROTA = 3  # referência/exibição -- valor típico de uma rota cheia dentro do orçamento de horas (ver ROTA_TEMPO_MAXIMO_HORAS); a trava real virou dinâmica, ver roteirizacao_dados.estimar_tempo_rota
VOLUME_MAXIMO_ROTA = 100
DISTANCIA_MAXIMA_ROTA_KM = 20
# Orçamento de horas por rota: TEMPO_NIVEL3_HORAS, TEMPO_PARADA_NORMAL_
# HORAS e ROTA_TEMPO_MAXIMO_HORAS vêm IMPORTADOS de roteirizacao_dados
# (25/08 -- antes eram cópias locais, e o estimador do badge era uma
# reimplementação; como este badge virou o portão automático da Fase 3,
# ele precisa bater exatamente com a trava que formou a rota, então usa
# roteirizacao_dados.estimar_tempo_rota direto -- ver _badges_trava).
ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"  # mesma base de criar_rotas_diarias.py
_coords_base_cache: tuple[float, float] | None | bool = None


def _dados_lalamove(config: dict) -> dict:
    """{agent_id, veiculo_padrao, veiculos:[{codigo,nome}]} pro seletor de
    veículo Lalamove no card da rota (Hugo, 29/08)."""
    cfg = config.get("lalamove", {}) or {}
    veiculos = [{"codigo": str(v.get("codigo") or "").upper(), "nome": str(v.get("nome") or v.get("codigo") or ""),
                 "service_type": str(v.get("service_type") or v.get("codigo") or "").upper()}
                for v in (cfg.get("veiculos") or []) if v.get("codigo")]
    # Catálogo de opcionais por service_type (espera, ajuda no carregamento,
    # refrigeração...) -- vem da API com cache de 24h em disco; indisponível
    # = tela sem os opcionais, nunca quebra o carregamento (Hugo, 30/08).
    try:
        from lalamove_integracao import catalogo_special_requests
        catalogo = catalogo_special_requests(config)
    except Exception as e:
        logger.warning(f"Catálogo de opcionais Lalamove indisponível: {e}")
        catalogo = {}
    return {
        "agent_id": int(cfg.get("agent_id_vuupt") or 0),
        "veiculo_padrao": str(cfg.get("veiculo_padrao") or "").upper() or (veiculos[0]["codigo"] if veiculos else ""),
        "veiculos": veiculos,
        "catalogo": catalogo,
    }


def _garantir_coords_base() -> tuple[float, float] | None:
    """Coordenada da base pro estimador de horas (perna base -> 1ª
    parada, 25/08): geocodifica UMA vez por processo (cache hit em
    dados.db, sem chamada nova) e registra em roteirizacao_dados.
    Falha vira None (estimador segue sem a perna da base, nunca quebra
    a tela) -- e não tenta de novo até o processo reiniciar."""
    global _coords_base_cache
    if _coords_base_cache is None:
        try:
            from geocodificacao import geocodificar
            gmaps_key = _carregar_config().get("google_maps", {}).get("api_key", "")
            _coords_base_cache = geocodificar(ENDERECO_BASE, gmaps_key) or False
        except Exception as e:
            logger.warning(f"Sem coordenada da base pro estimador de horas: {e}")
            _coords_base_cache = False
        if _coords_base_cache:
            definir_coords_base(*_coords_base_cache)
    return _coords_base_cache or None


# Pedido "grande": acima disso vira alerta visual nos cards e no resumo
# do futuro (pedido do Hugo, 12/08) -- um pedido desses sozinho já
# ocupa boa parte do VOLUME_MAXIMO_ROTA de uma rota e merece atenção
# na hora de montar o dia (veículo/rota própria).
LIMITE_ALERTA_CAIXAS = 70
# Fase 0 do roadmap de roteirização (Hugo, 22/08): badges que olham o
# LOTE INTEIRO do dia, não só o rascunho isolado -- ver _badges_lote.
DISTANCIA_ISOLAMENTO_KM = DISTANCIA_MAXIMA_ROTA_KM  # ponto de partida: mesma magnitude da trava de agrupamento (20km)
ISOLAMENTO_MAX_PARADAS = 2
MINIMO_ROTAS_PARA_MEDIANA = 3  # com menos rotas no lote, "2x fora da mediana" não tem base estatística que preste


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _badges_trava(rascunho: dict) -> list[str]:
    """Avisos visuais (não bloqueiam) quando o rascunho, do jeito que
    está AGORA, estouraria alguma trava de roteirizacao_dados.py::
    dividir_em_sublotes -- mesmos limites, só que como aviso em vez de
    impedimento (a edição manual pode ter motivo legítimo pra furar).

    Rascunho classificado como veículo grande (`tipo_veiculo`, ver
    regras/tipo_veiculo.py) troca as travas de paradas/caixas de última
    milha pelas do PRÓPRIO tipo (caixas máx e endereços diferentes máx)
    -- as travas de nível 3/4 não se aplicam a essas rotas (só entram
    nelas pedido nível 1/2/3, nunca nível 4, e o orçamento de horas foi
    pensado pro contexto de última milha).

    Nível 3 (pedido do Hugo, 20/08): não é mais um teto fixo de
    quantidade -- o aviso dispara quando o TEMPO ESTIMADO da rota passa
    de ROTA_TEMPO_MAXIMO_HORAS. Custo por parada (TEMPO_NIVEL3_HORAS,
    TEMPO_PARADA_NORMAL_HORAS) e deslocamento (perna da base, fator
    estrada, velocidade urbana/rodovia) calibrados pela execução real em
    25/08 -- ver o bloco de constantes e a docstring de
    roteirizacao_dados.estimar_tempo_rota pros valores atuais, não
    repetidos aqui de propósito pra não desatualizar de novo."""
    paradas = rascunho["paradas"]
    badges = []
    caixas = sum(p["volume_caixas"] or 1 for p in paradas)
    tipo_veiculo = tipo_por_codigo(rascunho.get("tipo_veiculo"))

    if tipo_veiculo:
        enderecos_distintos = {p["endereco"] for p in paradas}
        if caixas > tipo_veiculo.volume_maximo_cx:
            badges.append(f"{caixas} caixa(s) (máx {tipo_veiculo.volume_maximo_cx} p/ {tipo_veiculo.nome})")
        if len(enderecos_distintos) > tipo_veiculo.max_enderecos_distintos:
            badges.append(
                f"{len(enderecos_distintos)} endereços diferentes "
                f"(máx {tipo_veiculo.max_enderecos_distintos} p/ {tipo_veiculo.nome})"
            )
    else:
        if len(paradas) > TAMANHO_MAXIMO_ROTA:
            badges.append(f"{len(paradas)} paradas (máx {TAMANHO_MAXIMO_ROTA})")
        if caixas > VOLUME_MAXIMO_ROTA:
            badges.append(f"{caixas} caixa(s) (máx {VOLUME_MAXIMO_ROTA})")

        niveis = [p["nivel_dificuldade"] or 1 for p in paradas]
        if len(paradas) > 1 and any(n >= 4 for n in niveis):
            badges.append("entrega nível 4 dividindo rota com outras")
        else:
            # MESMO estimador que formou a rota (roteirizacao_dados.
            # estimar_tempo_rota: paradas calibradas + perna da base +
            # fator estrada + velocidade urbana/rodovia), na ordem em que
            # as paradas estão no rascunho. As paradas já carregam
            # lat/lng -- coords_fn evita re-geocodificar o endereço.
            pseudo_servicos = [
                {"_nivel_dificuldade": n, "latitude": p["latitude"], "longitude": p["longitude"]}
                for n, p in zip(niveis, paradas)
            ]
            tempo_estimado = estimar_tempo_rota(
                pseudo_servicos, coords_base=_garantir_coords_base(),
                coords_fn=lambda s: (s["latitude"], s["longitude"]) if s["latitude"] and s["longitude"] else None,
            )
            if tempo_estimado > ROTA_TEMPO_MAXIMO_HORAS:
                tempo_paradas = sum(TEMPO_NIVEL3_HORAS if n == 3 else TEMPO_PARADA_NORMAL_HORAS for n in niveis)
                qtd_nivel3 = sum(1 for n in niveis if n == 3)
                badges.append(
                    f"tempo estimado {tempo_estimado:.1f}h (máx {ROTA_TEMPO_MAXIMO_HORAS:.0f}h, "
                    f"{qtd_nivel3} nível 3, +{tempo_estimado - tempo_paradas:.1f}h deslocamento)"
                )

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


def _centroide_paradas(paradas: list[dict]) -> tuple[float, float] | None:
    coords = [(p["latitude"], p["longitude"]) for p in paradas if p["latitude"] and p["longitude"]]
    if not coords:
        return None
    return (sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords))


def _badges_lote(rascunhos: list[dict]) -> None:
    """Avisos visuais que só fazem sentido olhando o LOTE INTEIRO do
    dia (mediana de km/paradas entre as rotas, distância geográfica
    entre rotas) -- diferente de _badges_trava (por rascunho, travas
    fixas de dividir_em_sublotes reavaliadas isoladamente). MUTA
    rascunho["badges"] de cada item em `rascunhos` (precisa já ter
    sido populado por _badges_trava antes desta chamada -- ver
    buscar_dados_planejamento). Pedido do Hugo, 22/08 (Fase 0).

    Badge de ISOLAMENTO: rota com <= ISOLAMENTO_MAX_PARADAS paradas e
    a mais de DISTANCIA_ISOLAMENTO_KM da rota mais próxima do lote
    (pelo centroide de cada rota) -- sinal de que dividir_em_sublotes
    (ou edição manual) deixou um bolsão pequeno e distante pra trás.
    Não se aplica a rota Viagem (mesma exceção de _badges_trava --
    Viagem já é esperado estar longe da Grande SP).

    Badge de DISPARIDADE: km_estimado ou nº de paradas da rota é
    2x+ maior OU 2x+ menor que a mediana do dia -- sinal de rota fora
    do padrão do lote (pra cima ou pra baixo); pode ser legítimo (área
    isolada, pedido grande) ou desbalanceamento que vale revisar."""
    if len(rascunhos) < 2:
        return

    centroides = {r["id"]: _centroide_paradas(r["paradas"]) for r in rascunhos}

    for r in rascunhos:
        if r.get("tipo_rota") == "VIAGEM" or len(r["paradas"]) > ISOLAMENTO_MAX_PARADAS:
            continue
        centro = centroides.get(r["id"])
        if not centro:
            continue
        distancias = [
            _distancia_km(*centro, *centroides[outro["id"]])
            for outro in rascunhos
            if outro["id"] != r["id"] and centroides.get(outro["id"])
        ]
        if distancias and min(distancias) > DISTANCIA_ISOLAMENTO_KM:
            r["badges"].append(
                f"rota isolada: {len(r['paradas'])} parada(s) a {min(distancias):.0f}km "
                f"da rota mais próxima do lote (máx {DISTANCIA_ISOLAMENTO_KM:.0f}km)"
            )

    if len(rascunhos) < MINIMO_ROTAS_PARA_MEDIANA:
        return
    kms = [r["km_estimado"] for r in rascunhos if r.get("km_estimado")]
    qtds_paradas = [len(r["paradas"]) for r in rascunhos if r["paradas"]]
    mediana_km = statistics.median(kms) if kms else None
    mediana_paradas = statistics.median(qtds_paradas) if qtds_paradas else None

    for r in rascunhos:
        if mediana_km and r.get("km_estimado"):
            if r["km_estimado"] >= 2 * mediana_km or r["km_estimado"] <= mediana_km / 2:
                r["badges"].append(f"{r['km_estimado']:.1f}km fora do padrão do dia (mediana {mediana_km:.1f}km)")
        if mediana_paradas and r["paradas"]:
            n = len(r["paradas"])
            if n >= 2 * mediana_paradas or n <= mediana_paradas / 2:
                r["badges"].append(f"{n} parada(s) fora do padrão do dia (mediana {mediana_paradas:.0f})")


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


def _servico_para_pool(servico: dict, remetentes_por_id: dict[int, str],
                        nf_por_codigo: dict[str, str] | None = None,
                        tipo_area: str | None = None,
                        mapa_niveis: dict[str, int] | None = None,
                        mapa_horarios: dict[str, tuple[str, str]] | None = None,
                        ajustes_manuais: dict[str, dict] | None = None) -> dict:
    lat, lng = servico.get("latitude"), servico.get("longitude")
    agendado = _data_agendada(servico)
    codigo = servico.get("code", "")
    documento = (servico.get("customer") or {}).get("code", "")
    horario_inicio, horario_fim = horario_efetivo(documento, mapa_horarios or {}, ajustes_manuais or {})
    return {
        "agendado_para": agendado.isoformat() if agendado else None,
        "tipo_area": tipo_area,  # None | "sp_nao_atendido" | "fora_sp" (ver identificar_area_nao_atendida)
        "service_id": servico["id"],
        "codigo": codigo,
        "titulo": servico.get("title", ""),
        "endereco": servico.get("address", ""),
        "latitude": float(lat) if lat not in (None, "") else None,
        "longitude": float(lng) if lng not in (None, "") else None,
        "sender_id": servico.get("sender_id"),
        "remetente_nome": remetentes_por_id.get(servico.get("sender_id"), "Remetente não identificado"),
        "destinatario_nome": (servico.get("customer") or {}).get("name") or "",
        # nível e horário: ajuste manual > planilha BD_CLIENTES.xlsx >
        # padrão (Hugo, 22/08 -- ver regras/complexidade_entrega.py).
        # Substitui o extrair_nivel_dificuldade(servico) puro que ficava
        # sempre em 1 aqui, porque nada injeta '_nivel_dificuldade' num
        # item do pool antes deste ponto (só criar_rotas_diarias.py faz
        # isso, e só pra pedido que já virou rascunho).
        "nivel_dificuldade": nivel_efetivo(documento, mapa_niveis or {}, ajustes_manuais or {}),
        "volume_caixas": extrair_volume_caixas(servico),
        "horario_atendimento_inicio": horario_inicio,
        "horario_atendimento_fim": horario_fim,
        # NF já casada (documentos_processados) pro pedido, se houver --
        # Hugo, 14/08: buscar/adicionar pedido à rota pelo número da NF.
        # 'codigo' pode agrupar mais de um pedido combinado por vírgula
        # (achado 20/08) -- junta a NF de cada um.
        "numero_nf": ", ".join(filter(None, (
            (nf_por_codigo or {}).get(c, "") for c in _codigos_base_lista(codigo)
        ))),
    }


ROTULO_STATUS_RETIRADA = {
    "assigned": "Aguardando retirada",
    "accepted": "Aguardando retirada",
    "on_route": "Em andamento",
    "arrived": "Em andamento",
}


def _data_br(iso: str) -> str:
    """'2026-08-28' -> '28/08/2026'; devolve como veio se não for ISO."""
    partes = iso.split("-")
    return f"{partes[2]}/{partes[1]}/{partes[0]}" if len(partes) == 3 else iso


def _servico_para_retirada(servico: dict, remetentes_por_id: dict[int, str],
                           nf_por_codigo: dict[str, str] | None = None) -> dict:
    """Item do bloco "A retirar" -- só leitura (sem drag/seleção). O
    destinatário e a transportadora vêm do próprio título montado por
    retiradas.regras_retirada.montar_titulo:
    "[RETIRADA] #PS-x - ref / Embarcador / Destinatário / via Transp"."""
    titulo = (servico.get("title") or "").strip()
    sem_prefixo = titulo[len(PREFIXO_TITULO):].strip() if titulo.upper().startswith(PREFIXO_TITULO) else titulo
    partes = [p.strip() for p in sem_prefixo.split(" / ")]
    transportadora = next((p[4:] for p in partes if p.lower().startswith("via ")), "")
    meio = [p for p in partes[1:] if not p.lower().startswith("via ")]
    destinatario = meio[-1] if meio else ""
    codigo = servico.get("code", "")
    return {
        "service_id": servico["id"],
        "codigo": codigo,
        "titulo": sem_prefixo,
        "remetente_nome": remetentes_por_id.get(servico.get("sender_id"), "Remetente não identificado"),
        "destinatario_nome": destinatario,
        "transportadora": transportadora,
        "status": servico.get("status", ""),
        "status_rotulo": ROTULO_STATUS_RETIRADA.get(servico.get("status", ""), servico.get("status", "")),
        "criado_em": _data_br((servico.get("created_at") or "")[:10]),
        # previsão de expedição da Stokki, carimbada na nota do serviço
        # pela importação (pipeline) -- vazio pra serviço anterior a isso
        "data_prevista": data_prevista_do_servico(servico),
        "volume_caixas": extrair_volume_caixas(servico),
        "numero_nf": ", ".join(filter(None, (
            (nf_por_codigo or {}).get(c, "") for c in _codigos_base_lista(codigo)
        ))),
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


def _resumo_pedidos_agendados(servicos: list[dict]) -> list[dict]:
    """
    Agrega TODO pedido agendado e não finalizado (independente da data,
    pedido do Hugo, 12/08 -- antes era o "resumo do futuro", só os
    agendados pra depois da data em edição): um bloco por dia, em ordem
    cronológica, cada um com as regiões e seus totais de pedidos e
    caixas. Cada região traz também "grandes": os pedidos acima de
    LIMITE_ALERTA_CAIXAS, que viram alerta visual no card do dia. Dia
    já passado (< hoje) sai marcado com "atrasado": agendou, não
    finalizou e a data ficou pra trás.

    Quem decide o que entra é o chamador (buscar_pool_e_agendados
    junta not_assigned + accepted + on_route -- done/canceled ficam de
    fora); aqui só se exige agendamento válido: scheduled_start vazio
    ou malformado é pedido SEM agendamento (mesmo critério de
    elegivel_para_data), não tem dia pra aparecer no resumo.
    """
    hoje = date.today()
    por_dia: dict[date, dict[str, dict]] = {}
    for s in servicos:
        data_agendada = _data_agendada(s)
        if not data_agendada:
            continue  # sem agendamento (ou malformado) -- assunto do pool

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
            "atrasado": data_agendada < hoje,
            "total_pedidos": sum(r["pedidos"] for r in regioes),
            "total_caixas": sum(r["caixas"] for r in regioes),
            "regioes": regioes,
        })
    return resumo


def buscar_pool_e_agendados(data_alvo: date, config: dict | None = None) -> dict:
    """
    Busca ao vivo na VUUPT os pedidos e separa em:

      - "pool": TODO not_assigned que ainda não está em nenhum rascunho
        ATIVO (a coluna arrastável da tela) -- extraída de
        buscar_dados_planejamento() pra ser reaproveitada pelo botão
        "Atualizar" (Hugo, 12/08: verificar pedido novo chegando na
        VUUPT sem recarregar a página, perdendo a edição em andamento).
        Inclui também pedido agendado pra data FUTURA (pedido do Hugo,
        14/08: "sempre mostrar todos os pedidos, inclusive os
        agendados, com marcação visual diferente, mas permitindo
        alocação em rotas" -- antes esses ficavam de fora, via
        elegivel_para_data, e não tinha como adiantar uma entrega
        manualmente nem achar o pedido pela busca). O front marca esse
        caso (badge "Agendado dd/mm" cinza + urgência "futuro") e tem
        um filtro próprio pra ocultá-los do MAPA (não da lista) --
        ver "Agendados (futuro): no mapa" em planejamento_rotas.html.
        O pipeline automático (criar_rotas_diarias.py/incrementar_
        rotas.py) continua respeitando elegivel_para_data normalmente;
        só a tela manual de planejamento passou a mostrar tudo. Cada
        item do pool também traz "numero_nf" (rascunhos_rota.
        carregar_nf_por_codigo_pedido, casado pelo código BASE) quando
        já existe NF processada pro pedido -- entra na busca do front
        junto de código/remetente/destinatário/endereço (Hugo, 14/08).
        Cada item também traz "tipo_area" (None | "sp_nao_atendido" |
        "fora_sp", mesma classificação de notificar_area_nao_atendida.
        identificar_area_nao_atendida) -- pedido do Hugo, 20/08: pedido
        fora da área de atendimento (mesmo critério que o pipeline
        automático usa pra EXCLUIR da roteirização) precisa aparecer
        separado num 3º bloco da tela ("Fora da área"), não misturado em "pra
        rotear hoje" nem em "agendados pra depois" -- essa tela manual
        não tinha esse filtro antes, então um pedido assim ficava
        indistinguível dos demais no pool e podia ser arrastado pra uma
        rota sem ninguém perceber que era fora de área;
      - "resumo_agendados": TODO agendado não finalizado, independente
        da data, agregado por dia e região (_resumo_pedidos_agendados;
        pedido do Hugo, 12/08 -- substitui o antigo "resumo do futuro").
        Não finalizado = not_assigned (a mesma busca do pool) + accepted
        + on_route (2 buscas extras, leves: são só as paradas das rotas
        vivas do momento); done e canceled ficam de fora. Aqui NÃO se
        desconta quem está em rascunho: o resumo é a foto da demanda
        agendada, o pool é a bancada de trabalho;
      - "agendamentos_por_service_id": {service_id: data ISO} de TODO
        not_assigned com agendamento válido -- usado por
        buscar_dados_planejamento pra mostrar o agendamento também nas
        paradas já em rascunho (que continuam not_assigned na VUUPT até
        o envio), sem precisar de coluna nova em rascunhos_parada;
      - "tipos_area_por_service_id": {service_id: tipo_area} igual
        acima, mesmo motivo -- buscar_dados_planejamento usa pra marcar
        "tipo_area" também nas paradas já em rascunho (pedido pode ter
        sido colocado numa rota manualmente antes de ficar fora de área,
        ou por engano): o card continua mostrando o badge de alerta
        mesmo dentro da rota, e se for removido de volta pro pool, cai
        na seção certa ("Fora da área") em vez de "Pra rotear hoje".
    """
    config = config or _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    rascunhos_ativos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    ids_em_rascunho = {p["service_id"] for r in rascunhos_ativos for p in r["paradas"]}

    remetentes_por_id = carregar_remetentes_por_sender_id()
    vuupt = VuuptClient(token)
    filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
    servicos_brutos = vuupt.listar_servicos(filtro, per_page=100, include=["customer"])
    nf_por_codigo = rascunhos_rota.carregar_nf_por_codigo_pedido(
        {c for s in servicos_brutos for c in _codigos_base_lista(s.get("code", ""))})

    # nível de dificuldade / horário de atendimento por destinatário
    # (Hugo, 22/08 -- ver regras/complexidade_entrega.py e a opção "Nível
    # / horário de atendimento" do menu de contexto): carregado uma vez
    # por chamada, igual remetentes_por_id/nf_por_codigo acima.
    caminho_niveis = config.get("complexidade_entrega", {}).get("planilha", "")
    mapa_niveis = carregar_niveis(caminho_niveis)
    mapa_horarios = carregar_horarios(caminho_niveis)
    ajustes_manuais = carregar_ajustes_manuais()

    # Mesma classificação usada pelo pipeline automático pra excluir da
    # roteirização (roteirizacao/notificar_area_nao_atendida.py) -- aqui só
    # rotula o item do pool (tipo_area), não bloqueia nada: o Hugo continua
    # podendo arrastar manualmente se decidir atender mesmo assim.
    tipos_area: dict[int, str] = {}
    try:
        from notificar_area_nao_atendida import identificar_area_nao_atendida
        for s, tipo in identificar_area_nao_atendida(servicos_brutos, gmaps_key):
            tipos_area[s["id"]] = tipo
    except Exception as e:
        logger.warning(f"Falha ao classificar área não atendida pro pool (tela segue sem essa marcação): {e}")

    # Pool é sempre a FOTO AO VIVO do not_assigned na VUUPT -- não existe
    # "pool de um dia passado" (pedido do Hugo, 23/08: plano de dia
    # anterior a hoje é só consulta, sem editar/cancelar nem oferecer
    # pedido pra rotear ali). resumo_agendados/agendamentos/tipos_area
    # abaixo continuam calculados do jeito de sempre -- não são "o
    # Pool", e já eram os mesmos pra qualquer data_alvo antes desta trava.
    if data_alvo < date.today():
        pool = []
    else:
        pool = [
            _servico_para_pool(s, remetentes_por_id, nf_por_codigo, tipos_area.get(s["id"]),
                                mapa_niveis, mapa_horarios, ajustes_manuais)
            for s in servicos_brutos
            if s["id"] not in ids_em_rascunho
        ]
        pool.sort(key=lambda p: p["codigo"])

    agendamentos_por_service_id = {}
    for s in servicos_brutos:
        agendado = _data_agendada(s)
        if agendado:
            agendamentos_por_service_id[s["id"]] = agendado.isoformat()

    # Agendado já em rota (accepted/on_route) também é "não finalizado".
    # Falha aqui não derruba a tela: o resumo fica só com os
    # not_assigned, que continuam sendo a maior parte.
    servicos_resumo = list(servicos_brutos)
    for status_rota in ("accepted", "on_route"):
        try:
            servicos_resumo += vuupt.listar_servicos(
                [{"field": "status", "operator": "eq", "value": status_rota}], per_page=100)
        except Exception as e:
            logger.warning(f"Falha ao buscar serviços '{status_rota}' pro resumo de agendados: {e}")

    # Retiradas no galpão (Hugo, 28/08): bloco PRÓPRIO, fora do pool --
    # são serviços avulsos "[RETIRADA] ..." atribuídos ao agente fixo de
    # retiradas (status assigned/accepted/..., nunca not_assigned), então
    # nunca entram em rota nem no pool; a tela só mostra pra acompanhar.
    # Falha aqui não derruba a tela.
    pool_retiradas: list[dict] = []
    try:
        cfg_ret = config_retiradas(config)
        if cfg_ret["ativo"] and data_alvo >= date.today():
            servicos_ret = [s for s in vuupt.listar_servicos_do_agente(cfg_ret["agent_id"], STATUSES_ABERTOS)
                            if eh_servico_retirada(s)]
            nf_ret = rascunhos_rota.carregar_nf_por_codigo_pedido(
                {c for s in servicos_ret for c in _codigos_base_lista(s.get("code", ""))})
            pool_retiradas = [_servico_para_retirada(s, remetentes_por_id, nf_ret) for s in servicos_ret]
            pool_retiradas.sort(key=lambda p: p["codigo"])
    except Exception as e:
        logger.warning(f"Falha ao carregar retiradas no galpão pro planejamento (tela segue sem o bloco): {e}")

    return {
        "pool": pool,
        "pool_retiradas": pool_retiradas,
        "resumo_agendados": _resumo_pedidos_agendados(servicos_resumo),
        "agendamentos_por_service_id": agendamentos_por_service_id,
        "tipos_area_por_service_id": tipos_area,
    }


def buscar_dados_planejamento(data_alvo: date | None = None) -> dict:
    """
    Retorna {"data_alvo", "rascunhos": [...], "pool": [...],
    "resumo_agendados": [...], "base", "google_maps_key",
    "motoristas": [...]} pra tela de planejamento.

    Cada rascunho vem com "badges" (avisos de trava estourada) já
    calculados. O pool é TODO not_assigned que ainda não está em nenhum
    rascunho do lote ativo (inclusive agendado pra data futura, Hugo
    14/08 -- ver buscar_pool_e_agendados); resumo_agendados é todo
    pedido agendado não finalizado, por dia e região.
    """
    data_alvo = data_alvo or date.today()
    config = _carregar_config()
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    from geocodificacao import geocodificar
    coords_base = geocodificar(ENDERECO_BASE, gmaps_key)

    rascunhos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    confirmacoes_dia = listar_confirmacoes_do_dia(data_alvo)
    for r in rascunhos:
        r["badges"] = _badges_trava(r)
        # confirmação do motorista (página pública na VPS, ver
        # confirmacao_motoristas/) só existe pra rota já enviada à
        # VUUPT -- rascunho ainda pode trocar de motorista até lá.
        r["confirmacao_motorista"] = (
            confirmacoes_dia.get(r["agent_id"])
            if r["status"] == rascunhos_rota.STATUS_ENVIADO and r["agent_id"] is not None
            else None
        )
        # oferta do marketplace (Hugo, 22/08) só existe enquanto o
        # rascunho está OFERTADA -- depois de escolhida, o rascunho já
        # volta pra RASCUNHO com motorista preenchido (ver
        # rascunhos_rota.aplicar_escolha_motorista), então não tem
        # oferta pendente pra mostrar. STATUS_OFERTADA só existe na
        # versão de rascunhos_rota.py do marketplace, ainda não
        # commitada -- getattr com default None faz essa comparação
        # sempre dar False (nenhum rascunho "ofertado") em vez de
        # AttributeError, até o marketplace ser deployado de verdade
        # (achado 22/08: rodava em TODA carga de /planejamento assim
        # que o lote do dia tivesse pelo menos 1 rascunho -- derrubava
        # a tela inteira).
        if r["status"] == getattr(rascunhos_rota, "STATUS_OFERTADA", None):
            from regras import ofertas_rota
            oferta = ofertas_rota.buscar_por_rascunho(r["id"])
            r["oferta"] = {
                "resumo": json.loads(oferta["resumo_json"]),
                "qtd_elegiveis": len(json.loads(oferta["agent_ids_elegiveis"])),
            } if oferta else None
        else:
            r["oferta"] = None

    _badges_lote(rascunhos)

    pool_e_agendados = buscar_pool_e_agendados(data_alvo, config)

    # agendamento das paradas já em rascunho vem da MESMA busca do pool
    # (elas continuam not_assigned na VUUPT até o envio) -- pedido do
    # Hugo, 12/08: mostrar o agendamento também nos cards das rotas
    agendamentos = pool_e_agendados["agendamentos_por_service_id"]
    # tipo_area: mesmo princípio -- pedido do Hugo, 20/08: pedido fora
    # da área de atendimento continua marcado (badge de alerta) mesmo
    # se já estiver dentro de um rascunho, e volta pra seção certa do
    # pool ("Fora da área") se for removido da rota de novo.
    tipos_area = pool_e_agendados["tipos_area_por_service_id"]
    # NF: mesmo princípio, mas casada pelo código do pedido (não muda
    # com o envio) -- pedido do Hugo, 14/08: buscar pedido pela NF
    # também dentro de rotas já montadas, não só no pool
    nf_por_codigo_rascunho = rascunhos_rota.carregar_nf_por_codigo_pedido(
        {c for r in rascunhos for p in r["paradas"] for c in _codigos_base_lista(p["codigo"])})
    for r in rascunhos:
        for p in r["paradas"]:
            p["agendado_para"] = agendamentos.get(p["service_id"])
            p["tipo_area"] = tipos_area.get(p["service_id"])
            p["numero_nf"] = ", ".join(filter(None, (
                nf_por_codigo_rascunho.get(c, "") for c in _codigos_base_lista(p["codigo"])
            )))

    cfg_motoristas = config.get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
    motoristas = [
        {"agent_id": m.agent_id, "vehicle_id": m.vehicle_id, "nome": m.nome, "tipo_veiculo": m.tipo_veiculo,
         "ativo": m.ativo, "dias_disponiveis": m.dias_disponiveis}
        for m in catalogo.motoristas
    ]
    disponibilidade_ajustes = carregar_ajustes_dia(data_alvo)

    # Resumo do marketplace (Hugo, 23/08): total publicado + por região,
    # pro topo da tela -- computado aqui (não em JS) porque toda ação de
    # publicar/despublicar já recarrega a página inteira (location.reload
    # em planejamento_rotas.html), então o número sempre vem fresco.
    ofertadas = [r for r in rascunhos if r["oferta"]]
    contagem_regiao: dict[str, int] = {}
    for r in ofertadas:
        regiao = r["oferta"]["resumo"].get("regiao") or "Região não identificada"
        contagem_regiao[regiao] = contagem_regiao.get(regiao, 0) + 1
    resumo_ofertas = {
        "total": len(ofertadas),
        "por_regiao": sorted(contagem_regiao.items(), key=lambda item: (-item[1], item[0])),
    }

    return {
        "data_alvo": data_alvo.strftime("%d/%m/%Y"),
        "data_alvo_iso": data_alvo.isoformat(),
        "rascunhos": rascunhos,
        "pool": pool_e_agendados["pool"],
        "pool_retiradas": pool_e_agendados["pool_retiradas"],
        "resumo_agendados": pool_e_agendados["resumo_agendados"],
        "base": {"lat": coords_base[0], "lng": coords_base[1]} if coords_base else None,
        "google_maps_key": gmaps_key,
        "motoristas": motoristas,
        # Motorista virtual LALAMOVE + veículos do seletor do card (ver
        # lalamove_integracao.py e config.yaml lalamove.veiculos)
        "lalamove": _dados_lalamove(config),
        "resumo_ofertas": resumo_ofertas,
        "disponibilidade_ajustes": disponibilidade_ajustes,
        "limite_alerta_caixas": LIMITE_ALERTA_CAIXAS,
        # Limites das travas pro card de rota mostrar a ocupação como
        # barra ANTES de estourar (redesenho 13/08) -- os badges de
        # trava continuam sendo a palavra final (nível/distância não
        # viram barra).
        "travas": {
            "max_paradas": TAMANHO_MAXIMO_ROTA, "max_caixas": VOLUME_MAXIMO_ROTA,
            # Limites por tipo de veículo grande (ver regras/tipo_veiculo.py)
            # -- o card de uma rota classificada troca a barra de
            # paradas/caixas genérica pela capacidade do PRÓPRIO tipo.
            "tipos_veiculo": {
                t.codigo: {"nome": t.nome, "volume_maximo_cx": t.volume_maximo_cx,
                          "max_enderecos_distintos": t.max_enderecos_distintos}
                for t in TIPOS_VEICULO
            },
        },
        # {sender_id: "Seco"|"Refrigerado"|"Congelado"} pro chip do modo "só
        # número" mostrar a faixa de carga (Hugo, 14/08) -- sender_id sem
        # entrada aqui é tratado como Seco no cliente (mesmo padrão de
        # classificar_tipo_carga).
        "tipos_carga_por_sender": carregar_tipos_carga_por_sender(_DB_PATH),
    }


_PADRAO_INDICE_ROTA = re.compile(r"#(\d+)\s*$")


def roteirizar_selecionados(data_alvo: date, service_ids: list[int],
                            modelo_forcado: str | None = None,
                            max_paradas_por_rota: int | None = TAMANHO_MAXIMO_ROTA) -> dict:
    """
    Roda o criador de rotas (criar_rotas_diarias.roteirizar_para_
    rascunhos: partição Seco/Frio, seleção de modelo + 2-opt, motorista
    equitativo) SÓ com os pedidos selecionados no pool -- botão
    "Roteirizar" da tela (pedido do Hugo, 12/08). Os rascunhos gerados
    entram no lote ATIVO da data (não num lote novo, que esconderia os
    rascunhos já em edição -- a tela mostra sempre só o lote mais
    recente).

    `modelo_forcado` (Hugo, 15/08): nome de um esquema específico do
    Laboratório de Roteirização (ex. "CEP real") pra rodar em vez da
    comparação automática entre modelos -- ver selecao_modelo.
    escolher_melhor_modelo. `None` mantém o comportamento automático.

    `max_paradas_por_rota` (Hugo, 15/08): teto de pedidos por rota pra
    essa roteirização -- padrão é a mesma trava do pipeline automático
    (TAMANHO_MAXIMO_ROTA, 18); `None` remove o limite (repassado como
    está pra criar_rotas_diarias.roteirizar_para_rascunhos, que troca
    por um teto bem folgado -- as travas de volume/distância/nível
    continuam valendo do mesmo jeito).

    Os serviços são rebuscados AO VIVO na VUUPT (o pool da tela não
    carrega o customer.code, que a classificação de nível exige) --
    pedido selecionado que já saiu do not_assigned nesse meio-tempo, ou
    que outra aba já colocou em rascunho, fica de fora e é contado em
    "pedidos_indisponiveis".

    Numeração '#N' e equilíbrio de motoristas continuam do lote ativo
    (rascunhos ENVIADOS inclusive -- já são rotas reais do dia).

    Retorna {"rotas_criadas", "pedidos_roteirizados", "pedidos_indisponiveis"}.
    """
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")

    rascunhos_ativos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    ids_em_rascunho = {p["service_id"] for r in rascunhos_ativos for p in r["paradas"]}

    vuupt = VuuptClient(token)
    filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
    servicos_brutos = vuupt.listar_servicos(filtro, per_page=100, include=["customer"])
    por_id = {s["id"]: s for s in servicos_brutos}

    selecionados = [por_id[sid] for sid in service_ids if sid in por_id and sid not in ids_em_rascunho]
    indisponiveis = len(service_ids) - len(selecionados)
    if not selecionados:
        raise ValueError("Nenhum dos pedidos selecionados está disponível pra roteirizar "
                         "(já em rascunho ou não estão mais 'not_assigned' na VUUPT). "
                         "Atualize a página e tente de novo.")

    indice_inicial = 1
    contagem_alocacoes_dia: dict[int, int] = {}
    for r in rascunhos_ativos:
        m = _PADRAO_INDICE_ROTA.search(r["nome"] or "")
        if m:
            indice_inicial = max(indice_inicial, int(m.group(1)) + 1)
        if r.get("agent_id"):
            contagem_alocacoes_dia[r["agent_id"]] = contagem_alocacoes_dia.get(r["agent_id"], 0) + 1

    # import tardio de propósito: puxa o pipeline inteiro (selecao_modelo,
    # alocacao_motoristas, notificadores...) -- só paga esse custo quando
    # o botão é usado, não em todo GET /planejamento
    import criar_rotas_diarias

    rascunhos_novos = criar_rotas_diarias.roteirizar_para_rascunhos(
        selecionados, data_alvo, config,
        indice_inicial=indice_inicial,
        contagem_alocacoes_dia=contagem_alocacoes_dia,
        sufixo_label=" (seleção manual)",
        modelo_forcado=modelo_forcado,
        tamanho_maximo=max_paradas_por_rota,
    )
    if not rascunhos_novos:
        raise ValueError("O criador de rotas não gerou nenhuma rota pra essa seleção.")

    referencia = rascunhos_rota.referencia_para_rascunho_manual(data_alvo)
    rascunhos_rota.criar_lote_rascunhos(data_alvo, rascunhos_novos, lote_id=referencia["lote_id"])

    return {
        "rotas_criadas": len(rascunhos_novos),
        "pedidos_roteirizados": sum(len(r["sublote"]) for r in rascunhos_novos),
        "pedidos_indisponiveis": indisponiveis,
    }


def alocar_motoristas_rascunhos(data_alvo: date) -> dict:
    """
    Botão "Alocar motoristas" da tela (Hugo, 13/08): roda a MESMA
    alocação equitativa do pipeline (alocacao_motoristas.
    selecionar_motorista_equitativo -- Viagem exige ACEITA_VIAGENS,
    zona predominante, rodízio de placa, menor carga do dia) sobre os
    rascunhos do lote ativo que ainda estão SEM motorista, antes do
    envio à VUUPT -- pensado pras rotas montadas manualmente na tela
    ("+ Nova rota" / "Criar rota" da seleção), que nascem sem
    motorista, e pras que ficaram sem por falta de elegível na
    roteirização automática.

    Escolha manual é respeitada: rascunho que já tem agent_id NÃO é
    tocado (pra realocar um, troque o select dele pra "Sem motorista"
    e clique de novo). O equilíbrio parte das alocações já existentes
    no lote ativo (ENVIADOS inclusive -- já são rotas reais do dia),
    mesma convenção de roteirizar_selecionados.

    Retorna {"alocados": [{rascunho_id, nome, agent_id, vehicle_id,
    motorista_nome}], "sem_elegivel": [nomes], "ja_tinham": N,
    "sem_paradas": N}.
    """
    config = _carregar_config()
    gmaps_key = config.get("google_maps", {}).get("api_key", "")
    cfg_motoristas = config.get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
    ajustes_disponibilidade = carregar_ajustes_dia(data_alvo)

    rascunhos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    contagem_alocacoes_dia: dict[int, int] = {}
    for r in rascunhos:
        if r.get("agent_id"):
            contagem_alocacoes_dia[r["agent_id"]] = contagem_alocacoes_dia.get(r["agent_id"], 0) + 1

    alocados: list[dict] = []
    sem_elegivel: list[str] = []
    ja_tinham = sem_paradas = 0
    for r in rascunhos:
        if r["status"] != rascunhos_rota.STATUS_RASCUNHO:
            continue
        if r.get("agent_id"):
            ja_tinham += 1
            continue
        if not r["paradas"]:
            sem_paradas += 1
            continue
        # Formato bruto que os classificadores (zona/viagem/rodízio,
        # veículo grande) esperam: campo 'address' -- obter_coordenadas
        # geocodifica por ele, com cache já quente (as paradas foram
        # geocodificadas desses mesmos endereços ao entrar no rascunho).
        # 'dimension_3' precisa vir junto (classificar_tipo_veiculo lê o
        # volume real de cada parada -- sem isso, extrair_volume_caixas
        # cairia no fallback de 1 caixa por parada e classificaria
        # errado o tipo de veículo necessário).
        sublote = [{"address": p["endereco"], "dimension_3": p["volume_caixas"]} for p in r["paradas"]]
        motorista = selecionar_motorista_equitativo(
            sublote, data_alvo, catalogo.motoristas, contagem_alocacoes_dia, gmaps_key,
            ajustes_disponibilidade=ajustes_disponibilidade,
        )
        if not motorista:
            sem_elegivel.append(r["nome"])
            continue
        rascunhos_rota.trocar_motorista(r["id"], motorista.agent_id, motorista.vehicle_id, motorista.nome)
        contagem_alocacoes_dia[motorista.agent_id] = contagem_alocacoes_dia.get(motorista.agent_id, 0) + 1
        alocados.append({
            "rascunho_id": r["id"], "nome": r["nome"], "agent_id": motorista.agent_id,
            "vehicle_id": motorista.vehicle_id, "motorista_nome": motorista.nome,
        })

    return {"alocados": alocados, "sem_elegivel": sem_elegivel,
            "ja_tinham": ja_tinham, "sem_paradas": sem_paradas}


def desalocar_motoristas_rascunhos(data_alvo: date) -> dict:
    """
    Botão "Desalocar motoristas" da tela (Hugo, 15\08): oposto do
    "Alocar motoristas" -- limpa o motorista de todo rascunho do lote
    ativo que ainda está em RASCUNHO (não toca ENVIADO, que já saiu
    pra VUUPT com aquele motorista -- use "Cancelar todas as rotas"
    pra essas). Serve pra descartar de uma vez a sugestão automática
    e realocar do zero.

    Retorna {"desalocados": [{rascunho_id, nome}], "sem_motorista": N}.
    """
    rascunhos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    desalocados: list[dict] = []
    sem_motorista = 0
    for r in rascunhos:
        if r["status"] != rascunhos_rota.STATUS_RASCUNHO:
            continue
        if not r.get("agent_id"):
            sem_motorista += 1
            continue
        rascunhos_rota.trocar_motorista(r["id"], None, None, None)
        desalocados.append({"rascunho_id": r["id"], "nome": r["nome"]})

    return {"desalocados": desalocados, "sem_motorista": sem_motorista}


def _sublote_para_elegibilidade(paradas: list[dict]) -> list[dict]:
    """Mesmo formato mínimo usado em alocar_motoristas_rascunhos --
    'address' pra classificação de zona/viagem/rodízio (geocodifica via
    cache, ignora latitude/longitude já resolvidas no rascunho) e
    'dimension_3' pra classificar_tipo_veiculo (precisa do volume real
    de cada parada)."""
    return [{"address": p["endereco"], "dimension_3": p["volume_caixas"]} for p in paradas]


def publicar_oferta_rascunho(rascunho_id: int) -> dict:
    """
    Botão "Publicar para motoristas" (Hugo, 22/08): publica um rascunho
    SEM motorista pro marketplace de escolha aberta -- calcula quem é
    elegível (mesmo filtro de alocar_motoristas_rascunhos, ver
    alocacao_motoristas.listar_motoristas_elegiveis) e monta o resumo
    que eles vão ver (regras/resumo_oferta.montar_resumo), grava a
    oferta (regras/ofertas_rota.py) e muda o rascunho pra OFERTADA.

    Não envia nada à VUUPT nem escolhe motorista sozinho -- só deixa a
    rota visível pro grupo elegível. O aviso em si (e-mail/WhatsApp) é
    disparado por quem chama este endpoint (ver painel_agentes.py),
    depois de confirmar que a publicação teve sucesso.

    Retorna {"ok": True, "elegiveis": [MotoristaPreferencias...], "resumo": {...}}
    ou {"ok": False, "erro": "..."}.
    """
    from regras import ofertas_rota
    from regras.resumo_oferta import montar_resumo as montar_resumo_oferta
    from alocacao_motoristas import listar_motoristas_elegiveis

    rascunho = rascunhos_rota.buscar_rascunho(rascunho_id)
    if not rascunho:
        return {"ok": False, "erro": "Rascunho não encontrado."}
    if rascunho["status"] != rascunhos_rota.STATUS_RASCUNHO:
        return {"ok": False, "erro": f"Rascunho não está em edição (status={rascunho['status']})."}
    if rascunho.get("agent_id"):
        return {"ok": False, "erro": "Rascunho já tem motorista definido -- desaloque antes de publicar."}
    if not rascunho["paradas"]:
        return {"ok": False, "erro": "Rascunho sem paradas -- nada pra publicar."}

    data_alvo = date.fromisoformat(rascunho["data_alvo"])
    config = _carregar_config()
    gmaps_key = config.get("google_maps", {}).get("api_key", "")
    cfg_motoristas = config.get("motoristas", {})
    catalogo = CatalogoMotoristas.carregar(cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
    ajustes_disponibilidade = carregar_ajustes_dia(data_alvo)

    outras_rotas = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    contagem_alocacoes_dia: dict[int, int] = {}
    for r in outras_rotas:
        if r.get("agent_id"):
            contagem_alocacoes_dia[r["agent_id"]] = contagem_alocacoes_dia.get(r["agent_id"], 0) + 1

    sublote = _sublote_para_elegibilidade(rascunho["paradas"])
    elegiveis = listar_motoristas_elegiveis(
        sublote, data_alvo, catalogo.motoristas, contagem_alocacoes_dia, gmaps_key,
        ajustes_disponibilidade=ajustes_disponibilidade,
    )
    if not elegiveis:
        return {"ok": False, "erro": "Nenhum motorista elegível pra essa rota -- ninguém veria a oferta."}

    resumo = montar_resumo_oferta(rascunho["paradas"], gmaps_key)

    def _ultimos4(telefone):
        digitos = re.sub(r"\D", "", telefone or "")
        return digitos[-4:] if len(digitos) >= 4 else None

    ofertas_rota.criar_ou_atualizar_oferta(
        rascunho_id, data_alvo, resumo,
        [{"agent_id": m.agent_id, "telefone_ultimos4": _ultimos4(m.telefone), "cpf": m.cpf} for m in elegiveis],
    )
    rascunhos_rota.publicar_oferta(rascunho_id)

    return {"ok": True, "elegiveis": elegiveis, "resumo": resumo}


def publicar_ofertas_em_lote(data_alvo: date) -> dict:
    """Botão "Publicar pendentes" (Hugo, 22/08): chama
    publicar_oferta_rascunho pra todo rascunho RASCUNHO sem motorista do
    lote ativo da data. Retorna {"publicados": [{rascunho_id, nome,
    elegiveis}], "sem_elegivel": [nomes], "ja_tinham": N, "sem_paradas": N}."""
    rascunhos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    publicados: list[dict] = []
    sem_elegivel: list[str] = []
    ja_tinham = sem_paradas = 0
    for r in rascunhos:
        if r["status"] != rascunhos_rota.STATUS_RASCUNHO:
            continue
        if r.get("agent_id"):
            ja_tinham += 1
            continue
        if not r["paradas"]:
            sem_paradas += 1
            continue
        resultado = publicar_oferta_rascunho(r["id"])
        if not resultado["ok"]:
            sem_elegivel.append(r["nome"])
            continue
        publicados.append({
            "rascunho_id": r["id"], "nome": r["nome"],
            "elegiveis": resultado["elegiveis"], "resumo": resultado["resumo"],
        })

    return {"publicados": publicados, "sem_elegivel": sem_elegivel,
            "ja_tinham": ja_tinham, "sem_paradas": sem_paradas}


def despublicar_oferta_rascunho(rascunho_id: int) -> dict:
    """Botão "Despublicar" (Hugo, 22/08): desiste da publicação antes de
    qualquer motorista escolher. Se um motorista ganhou a corrida
    (escolheu entre o clique e a chegada aqui), a escolha prevalece --
    ver regras/ofertas_rota.cancelar_oferta."""
    from regras import ofertas_rota

    rascunho = rascunhos_rota.buscar_rascunho(rascunho_id)
    if not rascunho:
        return {"ok": False, "erro": "Rascunho não encontrado."}
    if rascunho["status"] != rascunhos_rota.STATUS_OFERTADA:
        return {"ok": False, "erro": f"Rascunho não está publicado (status={rascunho['status']})."}

    cancelou = ofertas_rota.cancelar_oferta(rascunho_id)
    if not cancelou:
        return {"ok": False, "erro": "Um motorista já escolheu essa rota -- aguarde a sincronização aplicar a escolha."}

    rascunhos_rota.despublicar_oferta(rascunho_id)
    return {"ok": True}


def despublicar_ofertas_em_lote(data_alvo: date) -> dict:
    """Botão "Cancelar publicações" (Hugo, 23/08): despublica de uma vez
    todo rascunho OFERTADA do lote ativo da data -- espelho de
    publicar_ofertas_em_lote. Não mexe em quem já foi ESCOLHIDA (mesma
    trava de sempre, ver despublicar_oferta_rascunho/
    regras.ofertas_rota.cancelar_oferta): só despublica quem ainda
    está ABERTA -- se um motorista ganhou a corrida antes desse clique
    chegar, a escolha dele prevalece."""
    rascunhos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
    despublicados: list[dict] = []
    ja_escolhida: list[str] = []
    for r in rascunhos:
        if r["status"] != rascunhos_rota.STATUS_OFERTADA:
            continue
        resultado = despublicar_oferta_rascunho(r["id"])
        if resultado["ok"]:
            despublicados.append({"rascunho_id": r["id"], "nome": r["nome"]})
        else:
            ja_escolhida.append(r["nome"])

    return {"despublicados": despublicados, "ja_escolhida": ja_escolhida}


def salvar_disponibilidade_dia(data_alvo: date, ajustes_brutos: dict) -> dict:
    """
    Tela "Disponibilidade de motoristas" (Hugo, 16/08): grava o
    snapshot completo dos checkboxes marcados/desmarcados naquele dia
    -- `ajustes_brutos` = {agent_id (str ou int): {"disponivel": bool,
    "motivo": str|None}}, exatamente como a tela envia. Essa tabela é a
    fonte que "Alocar motoristas" (e os jobs agendados) sempre
    respeitam (regras/disponibilidade_motoristas.py).
    """
    ajustes = {
        int(agent_id): (bool(v.get("disponivel")), (v.get("motivo") or None))
        for agent_id, v in ajustes_brutos.items()
    }
    definir_disponibilidade_dia(data_alvo, ajustes)
    return {"quantidade": len(ajustes)}


def marcar_disponibilidade_periodo(agent_id: int, data_inicio: date, data_fim: date,
                                   disponivel: bool, motivo: str | None) -> dict:
    """Mini-formulário "Marcar período" da tela de disponibilidade --
    ex. lançar férias/atestado de um motorista de uma vez em vários
    dias (Hugo, 16/08)."""
    quantidade = definir_disponibilidade_periodo(agent_id, data_inicio, data_fim, disponivel, motivo)
    return {"quantidade": quantidade}


def limpar_disponibilidade_dia(agent_id: int, data_alvo: date) -> dict:
    """Botão "Redefinir" de uma linha da tela de disponibilidade --
    remove o ajuste daquele motorista naquele dia, volta a valer o
    padrão semanal (DIAS_DISPONIVEIS)."""
    removido = limpar_ajuste(agent_id, data_alvo)
    return {"removido": removido}


def cancelar_pedido(service_id: int, rascunho_id: int | None = None) -> dict:
    """
    Cancela DE VERDADE um pedido na VUUPT (DELETE /services/{id} --
    VuuptClient.cancelar_servico) direto da tela de planejamento --
    botão "Cancelar pedido" (Hugo, 15/08). Cobre os 3 lugares onde um
    pedido pode estar quando o usuário clica:

      - No pool (not_assigned, fora de rascunho): cancela direto.
      - Numa rota ainda não enviada (rascunho RASCUNHO/ERRO_ENVIO):
        cancela e tira a parada do rascunho local (remover_parada).
      - Numa rota já enviada (rascunho ENVIADO, rota de verdade na
        VUUPT): rascunhos_rota.preparar_cancelamento_de_parada tira o
        serviço da rota de verdade primeiro (ou cancela a rota inteira,
        se for a última parada) -- só quando ela ainda não iniciou
        deslocamento (checado ao vivo); só então o serviço é cancelado
        de fato.

    NÃO mexe em nada na Stokki -- só cancela na VUUPT (roteirização).
    Cancelar o pedido na Stokki de verdade continua manual, fora dessa
    tela (não existe endpoint mapeado pra isso).

    Retorna {"ok": True} ou {"ok": False, "erro": "..."}.
    """
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")

    rascunho = rascunhos_rota.buscar_rascunho(rascunho_id) if rascunho_id else None
    if rascunho_id and not rascunho:
        return {"ok": False, "erro": f"Rascunho {rascunho_id} não encontrado."}

    if rascunho and rascunho["status"] == rascunhos_rota.STATUS_ENVIADO:
        preparo = rascunhos_rota.preparar_cancelamento_de_parada(rascunho_id, service_id, token)
        if not preparo["ok"]:
            return preparo

    try:
        VuuptClient(token).cancelar_servico(service_id)
    except VuuptAPIError as e:
        return {"ok": False, "erro": str(e)}

    if rascunho and rascunho["status"] != rascunhos_rota.STATUS_ENVIADO:
        rascunhos_rota.remover_parada(rascunho_id, service_id)

    return {"ok": True}


def reagendar_pedido(service_id: int, data: str, hora_inicio: str, hora_fim: str) -> dict:
    """
    Agenda/reagenda um pedido na VUUPT (PUT /services/{id}, campos
    scheduled_start/scheduled_end) -- opção "Agendar / reagendar" do
    menu de contexto da tela de planejamento (Hugo, 16/08).

    `data` no formato "YYYY-MM-DD" (input type=date), `hora_inicio`/
    `hora_fim` no formato "HH:MM" (input type=time) -- convertidos pro
    ISO8601 com offset de Brasília pelo mesmo conversor do reagendamento
    automático por e-mail (vuupt_client._converter_data_para_iso), pra
    não repetir o bug de fuso (sem offset explícito a API entende UTC e
    adianta o horário em 3h).

    NÃO mexe em nada na Stokki, só no agendamento da VUUPT -- e não
    reaplica as regras de dia fixo por região (essas são só pro
    reagendamento automático via e-mail); aqui é uma escolha manual do
    usuário, vai pra VUUPT como digitada.

    Retorna {"ok": True} ou {"ok": False, "erro": "..."}.
    """
    scheduled_start, scheduled_end, erro = _converter_janela_reagendamento(data, hora_inicio, hora_fim)
    if erro:
        return {"ok": False, "erro": erro}

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    try:
        VuuptClient(token).atualizar_servico(service_id, {
            "scheduled_start": scheduled_start,
            "scheduled_end": scheduled_end,
        })
    except VuuptAPIError as e:
        return {"ok": False, "erro": str(e)}

    return {"ok": True}


def _converter_janela_reagendamento(data: str, hora_inicio: str, hora_fim: str) -> tuple[str | None, str | None, str | None]:
    """Converte data + janela de horário (formato dos inputs date/time
    do modal) pro par scheduled_start/scheduled_end ISO8601 que a VUUPT
    espera -- compartilhado por reagendar_pedido e reagendar_pedidos
    (lote). Retorna (scheduled_start, scheduled_end, None) ou
    (None, None, mensagem_de_erro)."""
    # _converter_data_para_iso só aceita "%Y-%m-%d %H:%M:%S" (com
    # segundos) pro formato com espaço -- o <input type="time"> manda
    # "HH:MM" sem segundos, por isso completa aqui antes de converter.
    scheduled_start = _converter_data_para_iso(f"{data} {hora_inicio}:00")
    scheduled_end = _converter_data_para_iso(f"{data} {hora_fim}:00")
    if not scheduled_start or not scheduled_end:
        return None, None, f"Data/horário inválidos: {data} {hora_inicio}-{hora_fim}"
    return scheduled_start, scheduled_end, None


def reagendar_pedidos(itens: list[dict], data: str, hora_inicio: str, hora_fim: str) -> dict:
    """
    Versão em lote de reagendar_pedido -- aplica a MESMA janela de
    data/horário a todos os pedidos de `itens` (cada item:
    {"service_id": int}) -- botão "Agendar" da barra de seleção
    múltipla da tela de planejamento (Hugo, 20/08), tanto pra seleção
    do pool quanto pra seleção dentro de rotas.

    Falha num pedido NÃO aborta os demais -- cada um é tentado
    independentemente. Retorna {"ok": True, "falhas": [{"service_id":
    n, "erro": "..."}]}, com "falhas" vazia quando tudo deu certo. Só
    devolve {"ok": False} pra erro geral (data/hora inválida ou nenhum
    pedido selecionado), sem tentar nenhum.
    """
    scheduled_start, scheduled_end, erro = _converter_janela_reagendamento(data, hora_inicio, hora_fim)
    if erro:
        return {"ok": False, "erro": erro}
    if not itens:
        return {"ok": False, "erro": "Nenhum pedido selecionado."}

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)

    falhas = []
    for item in itens:
        service_id = int(item["service_id"])
        try:
            vuupt.atualizar_servico(service_id, {
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
            })
            logger.info(f"Agendamento em lote: serviço {service_id} atualizado.")
        except Exception as e:
            # Exception ampla (não só VuuptAPIError) de propósito: uma
            # falha de rede/timeout num item NÃO pode travar o loop e
            # deixar os itens seguintes sem sequer serem tentados --
            # achado 20/08 (Hugo reportou que o lote "não altera todos"),
            # o catch estreito deixava passar qualquer erro que não
            # fosse o VuuptAPIError da própria lib.
            logger.warning(f"Agendamento em lote: falha no serviço {service_id}: {e}")
            falhas.append({"service_id": service_id, "erro": str(e)})

    return {"ok": True, "falhas": falhas}


def _gravar_endereco_pedido(vuupt: VuuptClient, service_id: int, endereco: str,
                             dados_endereco: dict, rascunho_id: int | None) -> None:
    """
    Grava o endereço de UM pedido -- miolo compartilhado por
    editar_endereco_pedido (1 pedido) e editar_endereco_pedidos (lote).

    Grava com PUT /services/{id}, com address/latitude/longitude NO
    NÍVEL RAIZ do payload -- achado 20/08 (Hugo reportou que a edição
    "não impactava" a VUUPT): o SERVIÇO tem seus PRÓPRIOS campos
    address/latitude/longitude, um snapshot independente do endereço do
    'customer' (contato) vinculado -- mesmo padrão documentado pro
    phone_number em montar_payload_servico ("o serviço tem seu PRÓPRIO
    phone_number... independente do phone_number do contato... mesmo
    padrão da latitude/longitude, que também existe nos dois níveis").
    É esse campo do PRÓPRIO serviço que a VUUPT usa pro ponto de
    entrega/roteirização (confirmado: é dele que _servico_para_pool lê
    o "endereco" mostrado nos cards da tela) -- só atualizar o
    'customer' (como uma versão anterior desta função passou a fazer,
    corrigindo a confiabilidade do CADASTRO do contato) não move esse
    ponto. Nível raiz = mesmo mecanismo comprovado por reagendar_pedido
    com scheduled_start/scheduled_end, diferente do objeto 'customer'
    aninhado (esse sim documentado como não confiável em
    vuupt_client.resolver_customer_id).

    Depois, tenta sincronizar o mesmo endereço no 'customer' vinculado
    também (PUT /customers/{customer_id}, caminho confiável pra contato
    já existente -- ver resolver_customer_id) pra manter o cadastro
    coerente pra criações futuras. Isso é best-effort: falha aqui não
    propaga nem impede o restante, já que o efeito visível principal (o
    ponto da entrega) já foi gravado no passo acima.

    Se o pedido já está numa rota em rascunho (rascunho_id informado),
    também atualiza a cópia local em rascunhos_parada -- ela é lida ao
    vivo pela tela pra rotas já montadas, então sem isso o card
    continuaria mostrando o endereço antigo.

    Propaga VuuptAPIError da gravação principal (no serviço) pro
    chamador decidir como reportar -- é a única falha "fatal" aqui.
    """
    vuupt.atualizar_servico(service_id, dados_endereco)

    servico = vuupt.buscar_servico_por_id(service_id)
    customer_id = (servico or {}).get("customer_id")
    if customer_id:
        try:
            vuupt.atualizar_customer(customer_id, dados_endereco)
        except Exception as e:
            # Exception ampla de propósito (ver editar_endereco_pedidos):
            # isso é best-effort por design -- não pode propagar por
            # NENHUM tipo de erro, só VuuptAPIError, ou uma falha de
            # rede aqui derrubaria o pedido inteiro (e, em lote, todos
            # os itens seguintes) por causa só do passo secundário.
            logger.warning(
                f"Endereço do serviço {service_id} atualizado, mas falha ao "
                f"sincronizar o contato {customer_id}: {e}"
            )

    if rascunho_id is not None:
        rascunhos_rota.atualizar_endereco_parada(
            rascunho_id, service_id, endereco,
            dados_endereco.get("latitude"), dados_endereco.get("longitude"),
        )


def editar_endereco_pedido(service_id: int, endereco: str, rascunho_id: int | None = None) -> dict:
    """
    Edita o endereço de um pedido direto na VUUPT -- opção "Editar
    endereço" do menu de contexto da tela de planejamento (Hugo,
    18/08), mesmo padrão do "Agendar / reagendar" (reagendar_pedido).

    Regeocodifica o novo endereço (mesma geocodificacao.geocodificar
    usada na importação) e delega a gravação em si (serviço + contato +
    cópia local do rascunho) pro helper compartilhado
    _gravar_endereco_pedido -- ver docstring dele pros detalhes de POR
    QUE é assim (nível raiz do serviço, não o 'customer' aninhado). Se a
    geocodificação falhar (endereço não resolvido, sem API key etc.),
    envia só o texto do endereço; o VUUPT geocodifica por conta própria
    nesse caso.

    NÃO mexe em nada na Stokki, só no cadastro do pedido na VUUPT.

    Retorna {"ok": True} ou {"ok": False, "erro": "..."}.
    """
    endereco = (endereco or "").strip()
    if not endereco:
        return {"ok": False, "erro": "Endereço não pode ficar em branco."}

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)

    from geocodificacao import geocodificar
    gmaps_key = config.get("google_maps", {}).get("api_key", "")
    coords = geocodificar(endereco, gmaps_key)

    dados_endereco = {"address": endereco}
    if coords:
        dados_endereco["latitude"], dados_endereco["longitude"] = coords

    try:
        _gravar_endereco_pedido(vuupt, service_id, endereco, dados_endereco, rascunho_id)
    except Exception as e:
        # Exception ampla (não só VuuptAPIError): uma falha de
        # rede/timeout na chamada à VUUPT não pode virar um 500 cru pro
        # navegador -- vira um erro reportável igual qualquer outro.
        return {"ok": False, "erro": str(e)}

    return {"ok": True}


def editar_nivel_horario_pedido(service_id: int, nivel: int, horario_inicio: str,
                                 horario_fim: str, rascunho_id: int | None = None) -> dict:
    """
    Corrige o nível de dificuldade e/ou horário de atendimento (padrão de
    recebimento) do DESTINATÁRIO de um pedido -- opção "Nível / horário
    de atendimento" do menu de contexto da tela de planejamento (Hugo,
    22/08).

    Diferente de "Agendar / reagendar" (agendamento pontual de UM
    pedido, grava scheduled_start/end na VUUPT): isto aqui é uma
    característica do CLIENTE, guardada por documento (CNPJ/CPF) numa
    tabela local (regras.complexidade_entrega.definir_ajuste_manual) --
    vale pra TODOS os pedidos pendentes/futuros daquele destinatário, não
    só o pedido em que se clicou. NÃO grava nada na VUUPT: o campo
    equivalente lá (customer.operating_hour_start/end) é reescrito pelo
    pipeline de importação a partir de uma extração por LLM das mensagens
    de CADA pedido (regras/endereco.py::resolver_endereco_entrega), então
    seria sobrescrito silenciosamente numa importação futura.

    Busca o serviço vivo na VUUPT só pra descobrir o documento do
    destinatário (customer.code) -- não grava nada lá.

    Se `rascunho_id` for informado, também atualiza a cópia local da
    parada (rascunhos_rota.atualizar_nivel_horario_parada) pra quem já
    está numa rota não ficar mostrando o valor antigo até recarregar.

    Retorna {"ok": True} ou {"ok": False, "erro": "..."}.
    """
    if nivel not in NIVEIS_VALIDOS:
        return {"ok": False, "erro": f"Nível inválido: {nivel!r} (válidos: {sorted(NIVEIS_VALIDOS)})."}
    if not re.match(r"^\d{2}:\d{2}$", horario_inicio or "") or not re.match(r"^\d{2}:\d{2}$", horario_fim or ""):
        return {"ok": False, "erro": "Horário inválido (use HH:MM)."}

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)

    try:
        servico = vuupt.buscar_servico_por_id(service_id)
        customer_id = (servico or {}).get("customer_id")
        customer = vuupt.buscar_customer_por_id(customer_id) if customer_id else None
    except Exception as e:
        return {"ok": False, "erro": str(e)}

    # buscar_customer_por_id devolve o JSON cru da API, com o contato
    # aninhado sob a chave "customer" (diferente de buscar_servico_por_id,
    # que já desembrulha "service") -- mesmo idioma de desembrulho usado
    # em vuupt_client.py (ex: "customer = atualizado.get('customer',
    # atualizado)"). Achado 22/08 testando com dado real: sem isto,
    # documento sempre vinha vazio.
    customer = (customer or {}).get("customer", customer)
    documento = (customer or {}).get("code", "")
    if not documento:
        return {"ok": False, "erro": "Pedido sem CNPJ/CPF de destinatário cadastrado -- não dá pra saber de qual cliente é."}

    try:
        definir_ajuste_manual(documento, nivel, horario_inicio, horario_fim)
    except ValueError as e:
        return {"ok": False, "erro": str(e)}

    if rascunho_id is not None:
        rascunhos_rota.atualizar_nivel_horario_parada(rascunho_id, service_id, nivel, horario_inicio, horario_fim)

    return {"ok": True}


def editar_endereco_pedidos(itens: list[dict], endereco: str) -> dict:
    """
    Versão em lote de editar_endereco_pedido -- geocodifica o endereço
    UMA ÚNICA VEZ e grava o MESMO endereço em todos os pedidos de
    `itens` (cada item: {"service_id": int, "rascunho_id": int | None})
    -- botão "Editar endereço" da barra de seleção múltipla da tela de
    planejamento (Hugo, 20/08), tanto pra seleção do pool (rascunho_id
    sempre None) quanto pra seleção dentro de rotas.

    Falha num pedido NÃO aborta os demais -- cada um é tentado
    independentemente (ver _gravar_endereco_pedido). Retorna {"ok":
    True, "falhas": [{"service_id": n, "erro": "..."}]}, com "falhas"
    vazia quando tudo deu certo. Só devolve {"ok": False} pra erro geral
    (endereço vazio ou nenhum pedido selecionado), sem tentar nenhum.
    """
    endereco = (endereco or "").strip()
    if not endereco:
        return {"ok": False, "erro": "Endereço não pode ficar em branco."}
    if not itens:
        return {"ok": False, "erro": "Nenhum pedido selecionado."}

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)

    from geocodificacao import geocodificar
    gmaps_key = config.get("google_maps", {}).get("api_key", "")
    coords = geocodificar(endereco, gmaps_key)

    dados_endereco = {"address": endereco}
    if coords:
        dados_endereco["latitude"], dados_endereco["longitude"] = coords

    falhas = []
    for item in itens:
        service_id = int(item["service_id"])
        rascunho_id = item.get("rascunho_id")
        rascunho_id = int(rascunho_id) if rascunho_id is not None else None
        try:
            _gravar_endereco_pedido(vuupt, service_id, endereco, dados_endereco, rascunho_id)
            logger.info(f"Endereço em lote: serviço {service_id} atualizado.")
        except Exception as e:
            # Exception ampla (não só VuuptAPIError) de propósito: uma
            # falha de rede/timeout num item NÃO pode travar o loop e
            # deixar os itens seguintes sem sequer serem tentados --
            # achado 20/08 (Hugo reportou que o lote "não altera todos"),
            # o catch estreito deixava passar qualquer erro que não
            # fosse o VuuptAPIError da própria lib.
            logger.warning(f"Endereço em lote: falha no serviço {service_id}: {e}")
            falhas.append({"service_id": service_id, "erro": str(e)})

    return {"ok": True, "falhas": falhas}


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
        {"code": p["codigo"], "title": p["titulo"], "sender_id": p["sender_id"],
         # mesmos nomes de campo do serviço VUUPT: a capa em paisagem
         # mostra endereço de entrega e usa dimension_3 como fallback
         # de volumes quando a DANFE não dá a contagem real (13/08).
         "address": p.get("endereco") or "",
         "dimension_3": p.get("volume_caixas")}
        for p in rascunho["paradas"]
    ]
    codigos = {c for s in servicos for c in _codigos_base_lista(s["code"])}

    docs_por_pedido, _em_revisao = gpr.carregar_documentos_por_pedido(codigos)
    embarcadores, fatores = gpr.carregar_embarcadores()
    nome_motorista = rascunho.get("motorista_nome") or "(sem motorista)"
    rota_fake = {"name": rascunho["nome"], "id": rascunho["id"]}

    PASTA_ROMANEIOS_RASCUNHO.mkdir(parents=True, exist_ok=True)
    caminho_saida = PASTA_ROMANEIOS_RASCUNHO / f"rascunho_{rascunho_id}.pdf"

    gpr.montar_pdf_rota(rota_fake, servicos, docs_por_pedido, embarcadores,
                        fatores, nome_motorista, data_alvo, caminho_saida)
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
    codigos = sorted({c for p in rascunho["paradas"] if p["codigo"] for c in _codigos_base_lista(p["codigo"])})
    if not codigos:
        return {"JA_PROCESSADO": 0, "ENVIADO": 0, "REVISAO_MANUAL": 0, "ERRO": 0}

    return pdoc.main(modo_teste=False, pedidos_stokki=codigos, notificar=False)
