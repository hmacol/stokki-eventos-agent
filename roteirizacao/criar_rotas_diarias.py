# -*- coding: utf-8 -*-
"""
criar_rotas_diarias.py

Job agendado (originalmente das 13h, pedido do Hugo, 01/08 -- mas pode
rodar em qualquer horário, ver regra de data alvo abaixo): pega os
pedidos not_assigned do momento, agrupa geograficamente (mesma lógica
de roteirizacao_dados.py
-- região por coordenada real/CEP, consolidação até o mínimo de 10,
divisão balanceada até o máximo de 15), e CRIA uma rota de verdade no
VUUPT pra cada grupo -- via POST /routes (rotas_client.py), que
MATERIALIZA de fato (diferente do route-optimization, que só calcula).

Rotas saem SEM veículo/agente atribuído (pedido do Hugo: atribuição
manual depois, na tela). A sequência inicial dentro de cada rota usa a
ordem de proximidade calculada por dividir_em_sublotes, mas depois de
criar a rota, os pedidos são sequenciados por vizinho mais próximo +
2-opt/or-opt sem volta à base (roteirizacao_dados.py::ordenar_com_janelas,
Hugo 18/09; substituiu o "mais longe primeiro" de 03/08), que substituiu
a abordagem anterior (chamar o solver de route-optimization só pra sequenciar,
que existia porque rotas criadas via API não passam pelo
sequenciamento automático que a tela do VUUPT faz sozinha). Isso NÃO
decide quais pedidos entram (isso continua sendo decisão nossa, por
CEP/geocodificação) -- só define a ordem de visita.

Data alvo das rotas (pedido do Hugo, 10/08): processamento ANTES das
14h (horário de Brasília) cria rotas para o MESMO DIA; processamento
às 14h ou depois cria para o PRÓXIMO DIA ÚTIL (rola fim de semana pra
segunda, sem calendário de feriados -- mesmo critério simples já usado
em pipeline.py::_proximo_dia_util). O NOME da rota segue exatamente o
padrão nativo do VUUPT (mesma convenção usada pela própria tela,
confirmada com dado real): "Planejamento - DD/MM/AAAA - #N", numeração
GLOBAL pro dia (não por região) -- pra incrementar_rotas.py conseguir
encontrá-las depois.

COMO USAR:
    py -3.11 criar_rotas_diarias.py                  # execução normal (cria direto na VUUPT)
    py -3.11 criar_rotas_diarias.py --modo-teste      # só mostra o que criaria, não grava nada
    py -3.11 criar_rotas_diarias.py --gerar-rascunho  # grava rascunho local (dados/dados.db) para
                                                       # revisão/ajuste no painel de planejamento --
                                                       # modo padrão a partir de 12/08 (pedido do
                                                       # Hugo: nada vai pra VUUPT sem revisão manual)
"""
import argparse
import logging
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

# Força UTF-8 no stdout/stderr -- achado em produção, 06/08: sem isso,
# no Windows o console usa a codepage padrão (não UTF-8), e acentos
# (á, ç, ã...) saem corrompidos (mojibake tipo "Servi�o", "j� faz
# parte") tanto na tela quanto no log capturado pelo painel_agentes
# (que redireciona stdout+stderr pro arquivo de log da execução). O
# FileHandler abaixo já tinha encoding="utf-8" explícito, mas isso só
# corrigia o arquivo .log PRÓPRIO deste script -- não a saída que o
# painel captura via StreamHandler (que vai pro stderr por padrão).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "criar_rotas_diarias.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("criar_rotas_diarias")

import yaml

from vuupt_client import VuuptClient
from geocodificacao import geocodificar
from notificar_execucao_agente import notificar_execucao

from roteirizacao_dados import (
    elegivel_para_data, calcular_km_estimado, particionar_por_macro_regiao, caixas_e_enderecos,
    fundir_sublotes_pequenos, macro_regiao_predominante_do_sublote, MACRO_GRANDE_SP,
    definir_coords_base, reparar_sublotes_por_horas, ROTA_TEMPO_MAXIMO_HORAS,
    injetar_janelas, carregar_janelas_confirmadas, definir_hora_saida_base,
    start_at_rota, HORA_INICIO_ROTA, estimar_tempo_rota,
)
from selecao_modelo import escolher_melhor_modelo, agrupar_atual
from otimizacao_rotas import ordenar_2opt
from polimento_rotas import polir_entre_rotas
from rotas_client import criar_rota_removendo_conflitos
from fingerprint_rotas import marcar_alocado
from documentacao_rota import agendar as agendar_documentacao, aguardar as aguardar_documentacao
sys.path.insert(0, str(_RAIZ_PROJETO / "painel_agentes"))
from rascunhos_rota import criar_lote_rascunhos
from notificar_agendamento_pendente import identificar_pendentes, notificar_remetentes
from regras.clientes_agendamento import carregar_clientes_agendamento, tem_agendamento
from agendamento_confirmacao import buscar_confirmacao
from regioes_dia_fixo import aplicar_regioes_dia_fixo
from notificar_agendamento_dia_fixo import notificar_agendamentos_dia_fixo
from notificar_area_nao_atendida import (
    identificar_area_nao_atendida, notificar_remetentes as notificar_area_nao_atendida, EMAIL_TESTE,
)
from email_utils import notificacoes_automaticas_ativas
from regras.preferencias_motoristas import CatalogoMotoristas
from regras.disponibilidade_motoristas import carregar_ajustes_dia
from regras.complexidade_entrega import (
    carregar_niveis, classificar_nivel, carregar_horarios,
    carregar_ajustes_manuais, nivel_efetivo, horario_efetivo,
)
from regras.tipo_carga_embarcador import carregar_tipos_carga_por_sender, classificar_tipo_carga, TIPOS_CARGA_FRIA
from alocacao_motoristas import classificar_rota_viagem, selecionar_motorista_equitativo, contar_motoristas_elegiveis
from zonas_sp import classificar_rota_zona
from regras.tipo_veiculo import classificar_tipo_veiculo

ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"
BASE_LOCATION_ID = 6950  # confirmado em produção (operational_base_id da base, visto em dados reais do VUUPT)
DB_PATH = _RAIZ_PROJETO / "dados" / "dados.db"
TAMANHO_MINIMO_ROTA = 10
TAMANHO_MAXIMO_ROTA = 16  # Voltou de 14 para 16 entregas por rota (pedido do Hugo, 22/08 -- dado real de 21 dias mostrou que a imensa maioria das rotas nunca chega perto do teto, então a folga extra tem baixo risco; ver estimar_tempo_rota/VELOCIDADE_MEDIA_KMH pra rede de segurança de tempo, agora com deslocamento real embutido)
VOLUME_MAXIMO_ROTA = 100  # Novo limite máximo de caixas/volumes por rota (pedido do Hugo, 09/08)
# Máximo entre pedidos da mesma rota DENTRO da Grande SP (pedido do Hugo,
# 09/08 -- ajustado 10/08 pra 20). Hugo, 20/09: 15 km, calibrado pelo
# replay de 31 dias de operação real (roteirizacao/replay_rotas.py, ver
# DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md §9). Com 20 km o agrupador
# estica a rota e o diâmetro mediano PIORAVA (10,6 -> 11,1 km); 15 km é o
# único ponto medido em que todo indicador melhora ou fica estável
# (km -18,7%, diâmetro 10,1 km, entrelaçamento 19% contra 22%, zero rota
# acima de 9h), ao custo de ~1 rota a mais por dia. 12 km compacta mais
# (diâmetro 8,7 km) mas custa ~2 rotas por dia e 213 rotas pequenas.
DISTANCIA_MAXIMA_ROTA_KM = 15
# Rotas de Viagem (fora da Grande SP) NÃO têm limite de distância entre
# pedidos (pedido do Hugo, 10/08): as próprias regiões de dia fixo já
# têm vãos internos maiores que 15/20km (ex: Vale do Paraíba chega a
# ~55km entre Suzano e São José dos Campos) -- aplicar o mesmo limite
# urbano lá só fracionava a região inteira em várias rotas pequenas
# sem necessidade, já que é deslocamento longo de qualquer forma.
DISTANCIA_MAXIMA_VIAGEM_KM = None
# Teto de km ACUMULADO real da rota -- soma SEQUENCIAL dos trechos na
# ordem de formação (aproximação; ver dividir_em_sublotes/
# fundir_sublotes_pequenos em roteirizacao_dados.py), diferente do
# teto PAR-A-PAR acima (que só testa espalhamento máximo, não
# trajeto). Salvaguarda nova contra o caso extremo de zigzag que o
# par-a-par sozinho não pega (pedido do Hugo, 22/08 -- Fase 1 do
# roadmap de roteirização: ex. pedido de Niterói-RJ misturado com São
# Paulo, dentro de uma região de dia fixo com vãos internos legítimos
# de até ~55km).
KM_ACUMULADO_MAXIMO_ROTA_KM = 60      # Dentro da Grande SP
KM_ACUMULADO_MAXIMO_VIAGEM_KM = 300   # Viagem (fora da Grande SP) -- o teto PAR-A-PAR de Viagem acima continua None, deliberado (10/08)
# Fusão entre macro-regiões externas vizinhas, como ÚLTIMO RECURSO
# (pedido do Hugo, 15/08): quando uma região não junta o mínimo de
# pedidos nem somando os dois tipos de carga (ver
# _particionar_carga_com_fusao), tenta fundir com a OUTRA macro-região
# mais próxima -- só até este teto de distância entre centroides, senão
# fica isolada mesmo (medido em dado real, 15/08: Campinas<->Piracicaba
# 44km, mesmo corredor -- funde; Sorocaba<->Baixada Santista 128km,
# direções opostas mesmo caindo no mesmo dia fixo -- não funde).
DISTANCIA_MAXIMA_FUSAO_REGIAO_KM = 50
PREFIXO_NOME_ROTA = "Planejamento"
# Sentinela pro botão "Roteirizar" de Planejamento oferecer "sem limite"
# de pedidos por rota (Hugo, 15/08) -- as travas internas (dividir_em_
# sublotes, agrupar_por_*) sempre comparam contra um int, então "sem
# limite" vira esse teto folgado (nunca vai bater na prática: volume_
# maximo/distância/nível continuam valendo normalmente) em vez de um
# caminho de código à parte.
SEM_LIMITE_PARADAS = 10_000
# Particao por tipo de carga (Hugo, 18/09): DESLIGADA. Toda a frota tem
# bau com compartimento termico, entao Seco e Refrigerado/Congelado podem
# sempre ir no mesmo carro -- separar so criava rotas paralelas na mesma
# regiao (medido em 16-17/09: metade das paradas cuja vizinha mais
# proxima estava em OUTRA rota era por causa dessa particao). True volta
# ao comportamento anterior (Seco x Refrigerado x Misto por macro).
SEPARAR_POR_TIPO_CARGA = False
PARTICAO_GERAL = "Geral"
# Polimento entre rotas (Hugo, 18/09 -- ver polimento_rotas.py): roda
# depois do modelo vencedor e da fusao de sublotes pequenos. False =
# retorno rapido ao comportamento anterior. Teto de tempo por particao
# pra nao estourar a janela das 22h. 15.0s = medicao do controlador em
# 20/09 no maior dia de producao (18 rotas, 171 paradas): entre 3s e
# 15s o ganho e no entrelacamento (99% -> 61%), que e o sintoma
# relatado pelo Hugo; a curva de ganho de km satura por volta dos 15s
# (de 15s pra 30s o km melhora so ~1%). Desde a task anterior existe
# UMA particao por dia (nao tres), entao o teto e pago uma unica vez
# por execucao, e o job das 22h roda por timer sem restricao de tempo.
POLIMENTO_ATIVO = True
POLIMENTO_TEMPO_MAXIMO_S = 15.0
# Teto pro caminho INTERATIVO (fix final, 20/09): o botao "Roteirizar" da
# tela de Planejamento chama planejar_sublotes de forma SINCRONA, com uma
# pessoa esperando a resposta -- os 15s calibrados pro job noturno (que
# roda por timer, sem ninguem esperando) pesam demais ali. 5s ainda cobre
# a faixa de maior ganho (3-15s, ver comentario acima) sem travar a tela.
POLIMENTO_TEMPO_MAXIMO_INTERATIVO_S = 5.0


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _preparar_janelas(servicos: list[dict], config: dict) -> int:
    """Janela de horário de entrega por pedido (Hugo, 09/09): injeta
    '_janela_inicio/_fim/_fonte' em cada serviço (roteirizacao_dados.
    injetar_janelas -- agendamento confirmado com hora em
    agendamentos_pedido > scheduled_start/end real da Vuupt > horário de
    atendimento já injetado em '_horario_atendimento_*') e registra a
    hora de saída da base do simulador (config.yaml roteirizacao.
    hora_saida_base, padrão HORA_INICIO_ROTA = o start_at das rotas). Chamar DEPOIS
    do laço que injeta nível/horário de atendimento. Retorna quantos
    pedidos ficaram com janela (só pra log)."""
    definir_hora_saida_base((config.get("roteirizacao") or {}).get("hora_saida_base") or HORA_INICIO_ROTA)
    com_janela = injetar_janelas(servicos, carregar_janelas_confirmadas(DB_PATH))
    if com_janela:
        logger.info(f"{com_janela} de {len(servicos)} pedido(s) com janela de horário de entrega -- "
                    f"sequenciamento e travas vão respeitá-la.")
    return com_janela


TZ_BRASILIA = timezone(timedelta(hours=-3))
HORA_CORTE_MESMO_DIA = 14  # pedido do Hugo, 10/08


def _proximo_dia_util(data: date) -> date:
    """Rola a data para frente até cair em dia útil (seg-sex) -- mesmo
    critério simples (sem calendário de feriados) já usado em
    pipeline.py::_proximo_dia_util."""
    while data.weekday() >= 5:  # 5=sábado, 6=domingo
        data += timedelta(days=1)
    return data


def _data_alvo_rotas(agora: datetime) -> date:
    """
    Antes das 14h (horário de Brasília): rotas para o MESMO DIA do
    processamento. Às 14h ou depois: rotas para o PRÓXIMO DIA ÚTIL
    (pedido do Hugo, 10/08 -- antes disso a data alvo era sempre
    "amanhã", fixo, não importava o horário de execução).
    """
    if agora.hour < HORA_CORTE_MESMO_DIA:
        base = agora.date()
    else:
        base = agora.date() + timedelta(days=1)
    return _proximo_dia_util(base)


def _particionar_carga_com_fusao(servicos: list[dict], tamanho_minimo: int,
                                 gmaps_key: str | None) -> list[tuple[str, list[dict]]]:
    """
    Parte os serviços (já com '_tipo_carga' classificado) em até 3
    grupos -- Seco / Refrigerado-Congelado / Misto -- pra rotear em
    escolher_melhor_modelo (pedido do Hugo, 15/08: a frota tem baú com
    compartimento térmico, então dentro da MESMA macro-região, quando
    um dos dois tipos de carga não junta `tamanho_minimo` pedidos
    sozinho E o outro tipo também está presente ali, os dois entram
    juntos em "Misto (Seco+Refrigerado)" só NAQUELA macro -- em vez de
    2 rotas pequenas isoladas, 1 rota com volume suficiente. Achado
    real, 15/08: Campinas (Seco 5 + Refrigerado 6) vira 1 rota de 11
    em vez de 2 rotas pequenas.

    Macro-região com pedido suficiente de UM tipo (ou só um dos tipos
    presente ali) continua 100% separada, como sempre -- a fusão só
    entra quando ela realmente resolve uma rota pequena.

    Desde 18/09 (SEPARAR_POR_TIPO_CARGA=False) devolve UMA particao
    'Geral' com todos os servicos; o tipo de carga vira rotulo por rota
    (rotulo_carga).
    """
    if not SEPARAR_POR_TIPO_CARGA:
        return [(PARTICAO_GERAL, list(servicos))]

    macros = particionar_por_macro_regiao(servicos, api_key=gmaps_key)

    seco: list[dict] = []
    frio: list[dict] = []
    misto: list[dict] = []
    for lista in macros.values():
        lista_seco = [s for s in lista if s["_tipo_carga"] not in TIPOS_CARGA_FRIA]
        lista_frio = [s for s in lista if s["_tipo_carga"] in TIPOS_CARGA_FRIA]
        if lista_seco and lista_frio and (len(lista_seco) < tamanho_minimo or len(lista_frio) < tamanho_minimo):
            misto.extend(lista_seco)
            misto.extend(lista_frio)
        else:
            seco.extend(lista_seco)
            frio.extend(lista_frio)

    particoes = [("Seco", seco), ("Refrigerado/Congelado", frio)]
    if misto:
        particoes.append(("Misto (Seco+Refrigerado)", misto))
    return particoes


def rotulo_carga(sublote: list[dict]) -> str:
    """Rotulo de carga de UMA rota, derivado do conteudo (18/09): 'Seco',
    'Refrigerado/Congelado' ou 'Misto (Seco+Refrigerado)'. Gravado em
    rascunhos_rota.particao no lugar do nome da particao, pra tela,
    historico e filtros continuarem enxergando o mesmo vocabulario de
    antes. Rota vazia (nao acontece) rotula 'Seco'."""
    tem_frio = any(s.get("_tipo_carga") in TIPOS_CARGA_FRIA for s in sublote)
    tem_seco = any(s.get("_tipo_carga") not in TIPOS_CARGA_FRIA for s in sublote)
    if tem_frio and tem_seco:
        return "Misto (Seco+Refrigerado)"
    return "Refrigerado/Congelado" if tem_frio else "Seco"


def _fundir_sublotes_entre_macrorregioes(sublotes_do_dia: list[list[dict]], coords_base,
                                         gmaps_key: str | None, label: str) -> list[list[dict]]:
    """
    Fusão pós-hoc (Fase 1, 22/08 -- pedido do Hugo): ANTES de alocar
    motorista (nunca depois -- evita conflito de motorista já atribuído
    a um dos dois sublotes fundidos), tenta fundir cada sublote abaixo
    de TAMANHO_MINIMO_ROTA com o sublote mais próximo (por centroide)
    da MESMA macro-região dentro da MESMA partição de carga (Seco/
    Refrigerado/Misto -- só recebe sublotes de UMA partição por vez,
    quem chama já garante isso).

    sublotes_do_dia, neste ponto, é uma lista PLANA misturando sublotes
    de TODAS as macro-regiões da partição (selecao_modelo.py::_por_macro
    achata antes de devolver) -- recalcula a macro-região predominante
    de cada sublote (macro_regiao_predominante_do_sublote) antes de
    decidir compatibilidade -- nunca funde Grande SP com Viagem, nem
    duas regiões externas diferentes entre si.

    Reaproveita fundir_sublotes_pequenos (mesmo algoritmo do fix da
    Fase 0: candidatos por centroide, tenta o próximo mais próximo se o
    mais perto não couber), diferenciando o teto de distância/km
    acumulado por Grande SP x Viagem via eh_viagem_fn -- igual
    agrupar_atual faz.

    sublotes_do_dia já saiu de escolher_melhor_modelo/agrupar_atual
    SEQUENCIADO por ordenar_2opt -- fundir só concatena (receptor.
    extend), sem reotimizar o trajeto. Por isso, quando coords_base
    existe, roda ordenar_2opt de novo em TODOS os sublotes retornados
    sempre que HOUVE pelo menos 1 fusão.

    Orçamento de horas na ordem FINAL (25/08, achado da revisão
    adversarial): fundir_sublotes_pequenos aceita cada fusão pelo tempo
    estimado na ordem de CONCATENAÇÃO (perto->longe), mas o ordenar_2opt
    logo acima resequencia tudo por vizinho mais próximo + 2-opt/or-opt
    (sem volta à base, Hugo 18/09) -- a ordem final pode ficar bem
    diferente da concatenação, e uma fusão que cabia no tempo estimado
    por lá pode estourar na ordem final, sem ninguém reconferir depois
    (diferente de selecao_modelo.escolher_melhor_modelo, que já
    reparava só ANTES desta fusão cross-região).
    Mesmo padrão de dois passes de escolher_melhor_modelo: repara,
    resequencia de novo só quem foi reparado, repara de novo (sem 3º
    2-opt -- convergência esperada na 2ª rodada).
    """
    def _macro(sub):
        return macro_regiao_predominante_do_sublote(sub, gmaps_key)

    fundidos = fundir_sublotes_pequenos(
        sublotes_do_dia, TAMANHO_MINIMO_ROTA, TAMANHO_MAXIMO_ROTA, VOLUME_MAXIMO_ROTA,
        api_key=gmaps_key,
        distancia_maxima_km=DISTANCIA_MAXIMA_ROTA_KM,
        distancia_maxima_viagem_km=DISTANCIA_MAXIMA_VIAGEM_KM,
        km_acumulado_maximo=KM_ACUMULADO_MAXIMO_ROTA_KM,
        km_acumulado_maximo_viagem=KM_ACUMULADO_MAXIMO_VIAGEM_KM,
        eh_viagem_fn=lambda sub: _macro(sub) != MACRO_GRANDE_SP,
        compativel=lambda pequeno, candidato: _macro(pequeno) == _macro(candidato),
    )
    if len(fundidos) < len(sublotes_do_dia):
        logger.info(f"[{label}] Fusão entre macro-regiões: {len(sublotes_do_dia)} -> {len(fundidos)} sublote(s).")
        if coords_base:
            fundidos = [ordenar_2opt(s, coords_base[0], coords_base[1], gmaps_key) for s in fundidos]
            fundidos, reparadas = reparar_sublotes_por_horas(fundidos, gmaps_key)
            if reparadas:
                fundidos = [ordenar_2opt(s, coords_base[0], coords_base[1], gmaps_key) for s in fundidos]
                fundidos, _ = reparar_sublotes_por_horas(fundidos, gmaps_key)
                logger.warning(f"[{label}] Fusão entre macro-regiões: {reparadas} rota(s) passaram de "
                               f"{ROTA_TEMPO_MAXIMO_HORAS:.0f}h na ordem final -- quebradas pra caber no dia.")
    return fundidos


def _polir_particao(sublotes: list[list[dict]], coords_base, gmaps_key: str | None, label: str,
                    tamanho_maximo: int = TAMANHO_MAXIMO_ROTA,
                    tempo_maximo_s: float = POLIMENTO_TEMPO_MAXIMO_S) -> list[list[dict]]:
    """Polimento entre rotas de UMA particao (18/09). Sem base
    geocodificada ou com menos de 2 rotas nao ha o que polir."""
    if not POLIMENTO_ATIVO or not coords_base or len(sublotes) < 2:
        return sublotes
    try:
        polidos, resumo = polir_entre_rotas(
            sublotes, coords_base[0], coords_base[1], gmaps_key,
            tamanho_maximo=tamanho_maximo, volume_maximo=VOLUME_MAXIMO_ROTA,
            distancia_maxima_km=DISTANCIA_MAXIMA_ROTA_KM,
            distancia_maxima_viagem_km=DISTANCIA_MAXIMA_VIAGEM_KM,
            km_acumulado_maximo=KM_ACUMULADO_MAXIMO_ROTA_KM,
            km_acumulado_maximo_viagem=KM_ACUMULADO_MAXIMO_VIAGEM_KM,
            eh_viagem_fn=lambda sub: classificar_rota_viagem(sub, gmaps_key),
            tempo_maximo_s=tempo_maximo_s,
        )
    except Exception as e:
        # O polimento e a ULTIMA etapa e o resultado dele e totalmente
        # OPCIONAL -- um plano nao polido ja e um plano bom (fix final,
        # 20/09: antes uma excecao aqui subia sem tratamento e derrubava
        # a criacao de rotas da noite inteira). Devolve os sublotes de
        # entrada intocados; o resto do pipeline (job noturno ou tela de
        # Planejamento) segue normalmente sem o polimento.
        logger.exception(f"[{label}] Falha no polimento entre rotas (nao afeta o restante do planejamento -- "
                         f"seguindo com as rotas nao polidas): {e}")
        return sublotes
    logger.info(f"[{label}] Polimento entre rotas: {len(sublotes)} -> {len(polidos)} rota(s), "
                f"{resumo['realocacoes']} realocacao(oes), {resumo['trocas']} troca(s), "
                f"{resumo['esvaziadas']} esvaziada(s), km {resumo['km_antes']:.1f} -> {resumo['km_depois']:.1f} "
                f"em {resumo['tempo_s']:.1f}s{' (teto de tempo atingido)' if resumo['estourou_tempo'] else ''}.")
    return polidos


def planejar_sublotes(servicos: list[dict], coords_base, gmaps_key: str | None, data_alvo: date, *,
                      sufixo_label: str = "", modelo_forcado: str | None = None,
                      tamanho_maximo: int = TAMANHO_MAXIMO_ROTA,
                      polimento_tempo_maximo_s: float = POLIMENTO_TEMPO_MAXIMO_S,
                      registrar_historico: bool = True) -> list[dict]:
    """Miolo UNICO do criador de rotas (18/09): particao (tipo de carga,
    ver SEPARAR_POR_TIPO_CARGA) -> selecao diaria de modelo (ou fluxo de
    reserva sem base) -> fusao de sublotes pequenos entre macro-regioes
    -> polimento entre rotas. Usado por main() (job das 22h, teto de
    tempo POLIMENTO_TEMPO_MAXIMO_S), por roteirizar_para_rascunhos
    (botao Roteirizar da tela de Planejamento, sincrono, teto
    POLIMENTO_TEMPO_MAXIMO_INTERATIVO_S -- fix final, 20/09) e pelo
    replay (replay_rotas.py, com registrar_historico=False). Os servicos
    ja chegam classificados (_nivel_dificuldade, _tipo_carga, janelas) e
    a base, quando existe, ja foi registrada com definir_coords_base.
    Devolve [{"label", "modelo", "sublotes"}], uma entrada por particao
    nao vazia, sublotes ja sequenciados."""
    particoes = _particionar_carga_com_fusao(servicos, TAMANHO_MINIMO_ROTA, gmaps_key)
    planos: list[dict] = []
    for label, servicos_particao in particoes:
        if not servicos_particao:
            continue
        rotulo = f"{label}{sufixo_label}"
        if coords_base:
            modelo, sublotes = escolher_melhor_modelo(
                servicos_particao, coords_base[0], coords_base[1], gmaps_key,
                data_alvo=data_alvo, label=rotulo,
                tamanho_minimo=TAMANHO_MINIMO_ROTA, tamanho_maximo=tamanho_maximo,
                volume_maximo=VOLUME_MAXIMO_ROTA,
                distancia_maxima_km=DISTANCIA_MAXIMA_ROTA_KM,
                distancia_maxima_viagem_km=DISTANCIA_MAXIMA_VIAGEM_KM,
                modelo_forcado=modelo_forcado,
                distancia_maxima_fusao_regiao_km=DISTANCIA_MAXIMA_FUSAO_REGIAO_KM,
                km_acumulado_maximo=KM_ACUMULADO_MAXIMO_ROTA_KM,
                km_acumulado_maximo_viagem=KM_ACUMULADO_MAXIMO_VIAGEM_KM,
                registrar_historico=registrar_historico,
            )
        else:
            # Fluxo de reserva quando a base nao geocodifica -- mesmo
            # esquema "Atual", reaproveitado de selecao_modelo.py.
            modelo = "Atual (Grade+Greedy)"
            particoes_macro = particionar_por_macro_regiao(
                servicos_particao, gmaps_key, tamanho_minimo=TAMANHO_MINIMO_ROTA,
                distancia_maxima_fusao_km=DISTANCIA_MAXIMA_FUSAO_REGIAO_KM,
            )
            sublotes = [
                sub for svcs in particoes_macro.values()
                for sub in agrupar_atual(svcs, gmaps_key, TAMANHO_MINIMO_ROTA, tamanho_maximo,
                                         VOLUME_MAXIMO_ROTA, DISTANCIA_MAXIMA_ROTA_KM, DISTANCIA_MAXIMA_VIAGEM_KM,
                                         KM_ACUMULADO_MAXIMO_ROTA_KM, KM_ACUMULADO_MAXIMO_VIAGEM_KM)
            ]
        sublotes = _fundir_sublotes_entre_macrorregioes(sublotes, coords_base, gmaps_key, rotulo)
        sublotes = _polir_particao(sublotes, coords_base, gmaps_key, rotulo, tamanho_maximo,
                                   polimento_tempo_maximo_s)
        # Conferencia de cobertura POS fusao/polimento (fix final, 20/09):
        # a mesma checagem "nenhum pedido perdido, nenhum duplicado" que
        # selecao_modelo._validar ja faz roda ANTES da fusao e do
        # polimento -- exatamente as duas etapas onde um defeito real
        # desse tipo ja apareceu (polimento, achado so porque alguem
        # reproduziu a mao). So LOG, nunca excecao: numa noite de
        # producao um plano com 1 pedido a menos ainda e um plano
        # utilizavel, e o log e o que permite diagnosticar depois --
        # levantar excecao aqui derrubaria a criacao de rotas inteira
        # por causa de uma unica particao.
        ids_entrada = Counter(s["id"] for s in servicos_particao)
        ids_saida = Counter(s["id"] for sub in sublotes for s in sub)
        if ids_entrada != ids_saida:
            perdidos = sorted((ids_entrada - ids_saida).elements())
            duplicados = sorted((ids_saida - ids_entrada).elements())
            logger.error(f"[{rotulo}] [ALERTA_COBERTURA] Divergencia de cobertura apos fusao/polimento -- "
                        f"entrada {sum(ids_entrada.values())} pedido(s), saida {sum(ids_saida.values())} "
                        f"pedido(s). Perdido(s): {perdidos or 'nenhum'}. Duplicado(s): {duplicados or 'nenhum'}.")
        planos.append({"label": label, "modelo": modelo, "sublotes": sublotes})
    return planos


def roteirizar_para_rascunhos(servicos: list[dict], data_alvo: date, config: dict | None = None,
                              indice_inicial: int = 1,
                              contagem_alocacoes_dia: dict[int, int] | None = None,
                              sufixo_label: str = "",
                              modelo_forcado: str | None = None,
                              tamanho_maximo: int | None = TAMANHO_MAXIMO_ROTA) -> list[dict]:
    """
    Miolo do criador de rotas (classificação de nível/tipo de carga,
    planejar_sublotes (partição, seleção de modelo, fusão, polimento),
    alocação equitativa de motorista) aplicado a uma lista EXPLÍCITA de
    serviços brutos da VUUPT, devolvendo rascunhos prontos pra
    rascunhos_rota.criar_lote_rascunhos -- botão "Roteirizar" da seleção
    do pool na tela de planejamento (pedido do Hugo, 12/08: escolher
    pedidos e rodar o criador de rotas só com eles).

    Diferente do main(): não busca nada na VUUPT (a seleção JÁ é a
    entrada), não filtra elegibilidade (selecionar na tela é decisão
    explícita, vence o agendamento) e não dispara nenhuma notificação
    (dia fixo/agendamento pendente/área não atendida são assunto do job
    agendado). `indice_inicial` e `contagem_alocacoes_dia` vêm de quem
    chama, pra continuar a numeração '#N' e o equilíbrio de motoristas
    do lote já existente do dia (a contagem é MUTADA aqui, +1 por rota
    gerada). `sufixo_label` distingue as linhas do histórico de seleção
    de modelo das do job diário.

    `tamanho_maximo` (Hugo, 15/08): teto de pedidos por rota pro botão
    "Roteirizar" da tela de Planejamento -- padrão é o mesmo
    TAMANHO_MAXIMO_ROTA do pipeline automático (14), mas o usuário pode
    apertar (rotas menores) ou passar `None` explicitamente pra "sem
    limite" (vira SEM_LIMITE_PARADAS por baixo -- as outras travas,
    volume/distância/nível, continuam valendo do mesmo jeito).
    """
    config = config or _carregar_config()
    gmaps_key = config.get("google_maps", {}).get("api_key", "")
    tamanho_maximo_efetivo = tamanho_maximo if tamanho_maximo is not None else SEM_LIMITE_PARADAS

    cfg_motoristas = config.get("motoristas", {})
    catalogo_motoristas = CatalogoMotoristas.carregar(
        cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""),
    )
    ajustes_disponibilidade = carregar_ajustes_dia(data_alvo)
    contagem = contagem_alocacoes_dia if contagem_alocacoes_dia is not None else {}

    data_alvo_br = data_alvo.strftime("%d/%m/%Y")
    start_at = start_at_rota(data_alvo)  # HORA_INICIO_ROTA (06:00 BRT, Hugo 09/09)

    # mesma classificação do main(): nível de dificuldade (CNPJ do
    # destinatário) e tipo de carga (sender_id), injetados no dict
    caminho_niveis = config.get("complexidade_entrega", {}).get("planilha", "")
    mapa_niveis = carregar_niveis(caminho_niveis)
    mapa_horarios = carregar_horarios(caminho_niveis)
    ajustes_manuais = carregar_ajustes_manuais()
    mapa_tipos_carga = carregar_tipos_carga_por_sender(DB_PATH)
    for s in servicos:
        cnpj_destino = (s.get("customer") or {}).get("code", "")
        s["_nivel_dificuldade"] = nivel_efetivo(cnpj_destino, mapa_niveis, ajustes_manuais)
        s["_horario_atendimento_inicio"], s["_horario_atendimento_fim"] = \
            horario_efetivo(cnpj_destino, mapa_horarios, ajustes_manuais)
        tipo_carga, _ = classificar_tipo_carga(s.get("sender_id"), mapa_tipos_carga)
        s["_tipo_carga"] = tipo_carga
    # janela de horário do cliente (Hugo, 09/09) -- agendamento confirmado
    # com hora > scheduled_* real > horário de atendimento acima
    _preparar_janelas(servicos, config)

    coords_base = None
    try:
        coords_base = geocodificar(ENDERECO_BASE, gmaps_key)
    except Exception as e:
        logger.warning(f"Não consegui geocodificar a base -- seguindo com o agrupamento fixo: {e}")
    if coords_base:
        # orçamento de horas passa a contar a perna base -> 1ª parada
        # (25/08) em todo agrupamento/fusão deste processo
        definir_coords_base(*coords_base)

    planos = planejar_sublotes(servicos, coords_base, gmaps_key, data_alvo, sufixo_label=sufixo_label,
                               modelo_forcado=modelo_forcado, tamanho_maximo=tamanho_maximo_efetivo,
                               # botao "Roteirizar" e SINCRONO (pessoa esperando na tela) -- teto de
                               # tempo mais curto que o do job noturno (fix final, 20/09)
                               polimento_tempo_maximo_s=POLIMENTO_TEMPO_MAXIMO_INTERATIVO_S)

    indice = indice_inicial
    rascunhos: list[dict] = []
    for plano in planos:
        label, sublotes = plano["label"], plano["sublotes"]
        for sublote in sublotes:
            nome_rota = f"{PREFIXO_NOME_ROTA} - {data_alvo_br} - #{indice}"
            indice += 1
            eh_viagem = classificar_rota_viagem(sublote, gmaps_key)
            zona = None if eh_viagem else classificar_rota_zona(sublote, gmaps_key)
            tipo_veiculo = classificar_tipo_veiculo(*caixas_e_enderecos(sublote))
            motorista = selecionar_motorista_equitativo(
                sublote, data_alvo, catalogo_motoristas.motoristas, contagem, gmaps_key,
                ajustes_disponibilidade=ajustes_disponibilidade,
            )
            if motorista:
                contagem[motorista.agent_id] = contagem.get(motorista.agent_id, 0) + 1
            km_estimado = (
                calcular_km_estimado(sublote, coords_base[0], coords_base[1], gmaps_key)
                if coords_base else None
            )
            horas_sublote = estimar_tempo_rota(sublote, gmaps_key, coords_base)
            rascunhos.append({
                "nome": nome_rota,
                "particao": rotulo_carga(sublote),
                "tipo_rota": "VIAGEM" if eh_viagem else "GRANDE_SP",
                "zona": zona,
                "tipo_veiculo": tipo_veiculo.codigo if tipo_veiculo else None,
                "agent_id": motorista.agent_id if motorista else None,
                "vehicle_id": motorista.vehicle_id if motorista else None,
                "motorista_nome": motorista.nome if motorista else None,
                "start_location_base_id": BASE_LOCATION_ID,
                "end_location_base_id": BASE_LOCATION_ID,
                "start_at": start_at,
                "km_estimado": km_estimado,
                "horas_estimadas": round(horas_sublote, 2),
                "sublote": sublote,
            })
            veiculo_str = f" [veículo: {tipo_veiculo.nome}]" if tipo_veiculo else ""
            logger.info(f"[SELEÇÃO] [{label}] '{nome_rota}' [{'VIAGEM' if eh_viagem else 'GRANDE_SP'}]{veiculo_str} "
                       f"com {len(sublote)} pedido(s) -- motorista sugerido: "
                       f"{motorista.nome if motorista else 'SEM MOTORISTA [ALERTA_ALOCACAO]'}: "
                       f"{[s.get('code') for s in sublote]}")
    return rascunhos


def main(modo_teste: bool = False, gerar_rascunho: bool = False):
    inicio = time.time()
    prefixo_log = "[MODO TESTE] " if modo_teste else ("[RASCUNHO] " if gerar_rascunho else "")
    logger.info(f"{prefixo_log}Criação de rotas diárias iniciada.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    cfg_motoristas = config.get("motoristas", {})
    catalogo_motoristas = CatalogoMotoristas.carregar(
        cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""),
    )
    logger.info(f"{len(catalogo_motoristas.motoristas)} motorista(s) carregado(s) do catálogo de preferências.")
    contagem_alocacoes_dia: dict[int, int] = {}
    rotas_sem_motorista = 0

    resumo_etapas = {}

    try:
        vuupt = VuuptClient(token)

        agora_brasilia = datetime.now(TZ_BRASILIA)
        data_alvo = _data_alvo_rotas(agora_brasilia)
        data_alvo_br  = data_alvo.strftime("%d/%m/%Y")  # formato usado no NOME da rota (convenção nativa do VUUPT)
        ajustes_disponibilidade = carregar_ajustes_dia(data_alvo)
        logger.info(
            f"Processamento às {agora_brasilia.strftime('%H:%M')} (Brasília) -- "
            f"rotas para {data_alvo_br} "
            f"({'mesmo dia' if data_alvo == agora_brasilia.date() else 'próximo dia útil'})."
        )

        filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
        servicos_brutos = vuupt.listar_servicos(filtro, per_page=100, include=["customer"])
        logger.info(f"{len(servicos_brutos)} serviço(s) 'not_assigned' encontrado(s).")

        # Regiões com dia fixo de entrega (pedido do Hugo, 02/08) --
        # roda ANTES de tudo: define scheduled_start pra quem ainda
        # não tem e mora numa cidade de dia fixo, refletindo já no
        # dict em memória (o resto do fluxo, incluindo o filtro de
        # elegibilidade abaixo, já lida com isso automaticamente).
        try:
            agendados_dia_fixo = aplicar_regioes_dia_fixo(servicos_brutos, vuupt)
            if agendados_dia_fixo:
                logger.info(f"{len(agendados_dia_fixo)} pedido(s) agendado(s) por região de dia fixo.")
                # Avisa o remetente da data agendada (pedido do Hugo, 12/08).
                if notificacoes_automaticas_ativas(config):
                    resultado_aviso = notificar_agendamentos_dia_fixo(
                        agendados_dia_fixo, config.get("email", {}), modo_teste=modo_teste)
                    logger.info(f"Notificação de agendamento por dia fixo: {resultado_aviso}")
        except Exception as e:
            logger.error(f"Falha ao aplicar regiões de dia fixo (não afeta a criação de rotas): {e}")

        # Área não atendida (pedido do Hugo, 02/08): pedido fora de
        # toda região com dia fixo E fora do raio da Grande SP (ou
        # fora do estado de SP) -- notifica o remetente (cotação ou
        # confirmação de redespacho, dependendo do caso) e fica de
        # fora da rota até ser resolvido manualmente.
        ids_area_nao_atendida = set()
        try:
            pendentes_area = identificar_area_nao_atendida(servicos_brutos, gmaps_key)
            logger.info(f"{len(pendentes_area)} pedido(s) em área não atendida (SP fora do raio ou fora do estado).")
            ids_area_nao_atendida = {s["id"] for s, _tipo in pendentes_area}
            if pendentes_area:
                # Redirecionado só pro Hugo por enquanto (pedido dele, 20/08,
                # junto com a redução do raio da Grande SP pra 35km) -- quer
                # acompanhar manualmente antes de deixar ir direto pro cliente.
                resultado_area = notificar_area_nao_atendida(pendentes_area, config.get("email", {}),
                                                              modo_teste=modo_teste, forcar_destino=EMAIL_TESTE)
                logger.info(f"Notificação de área não atendida: {resultado_area}")
        except Exception as e:
            logger.error(f"Falha ao notificar área não atendida (não afeta a criação de rotas): {e}")

        # Notifica o remetente quando o destinatário exige agendamento
        # mas o agendamento está em branco ou vencido (pedido do Hugo,
        # 02/08) -- roda sobre a lista bruta (independe de estar
        # elegível pra rota de amanhã ou não). Pedidos que já têm
        # confirmação recebida (aguardando só o agente de atualização
        # aplicar no VUUPT) não entram como pendentes de novo.
        ids_pendentes_notificacao = set()
        try:
            caminho_agendamento = config.get("clientes_agendamento", {}).get("planilha", "")
            conjunto_agendamento = carregar_clientes_agendamento(caminho_agendamento)
            pendentes = identificar_pendentes(servicos_brutos, conjunto_agendamento, vuupt, tem_agendamento,
                                             buscar_confirmacao_fn=buscar_confirmacao)
            logger.info(f"{len(pendentes)} pedido(s) com agendamento pendente/vencido (destinatário exige).")
            ids_pendentes_notificacao = {s["id"] for s in pendentes}
            if pendentes and notificacoes_automaticas_ativas(config):
                resultado_notif = notificar_remetentes(pendentes, config.get("email", {}), modo_teste=modo_teste)
                logger.info(f"Notificação de agendamento pendente: {resultado_notif}")
        except Exception as e:
            logger.error(f"Falha ao notificar agendamento pendente (não afeta a criação de rotas): {e}")

        # Filtra agendamento (pedido do Hugo, 02/08): pedido agendado
        # pra data FUTURA (diferente de amanhã) não entra na rota de
        # amanhã -- espera o dia certo. Pedidos que acabamos de
        # notificar (agendamento em branco/vencido, sem resposta ainda,
        # ou área não atendida) TAMBÉM não entram (pedido do Hugo,
        # 02/08: "não adicionar na rota, aguardar resposta do e-mail").
        servicos = [
            s for s in servicos_brutos
            if elegivel_para_data(s, data_alvo)
            and s["id"] not in ids_pendentes_notificacao
            and s["id"] not in ids_area_nao_atendida
        ]
        adiados = len(servicos_brutos) - len(servicos)
        if adiados:
            logger.info(f"{adiados} pedido(s) com agendamento futuro, pendente de resposta, ou área não atendida -- adiados.")

        if not servicos:
            resumo_etapas["Criação de rotas"] = {"status": "ok", "detalhe": "Nenhum pedido not_assigned elegível."}
            return

        # Classificação por tipo de carga (Seco/Refrigerado/Congelado, via
        # sender_id -> tabela 'interno') e nível de dificuldade de entrega
        # (1-4, via CNPJ/CPF do destinatário -> planilha de complexidade) --
        # injetados no próprio dict do serviço (chaves '_tipo_carga' e
        # '_nivel_dificuldade') pra ficarem disponíveis em todo o resto do
        # fluxo (agrupamento, divisão em sublotes -- ver dividir_em_sublotes
        # em roteirizacao_dados.py) sem precisar repassar como parâmetro.
        caminho_niveis = config.get("complexidade_entrega", {}).get("planilha", "")
        mapa_niveis = carregar_niveis(caminho_niveis)
        mapa_horarios = carregar_horarios(caminho_niveis)
        ajustes_manuais = carregar_ajustes_manuais()
        mapa_tipos_carga = carregar_tipos_carga_por_sender(DB_PATH)
        cnpjs_pendentes_nivel = set()
        for s in servicos:
            cnpj_destino = (s.get("customer") or {}).get("code", "")
            _, _, nivel_requer_revisao = classificar_nivel(cnpj_destino, mapa_niveis)
            s["_nivel_dificuldade"] = nivel_efetivo(cnpj_destino, mapa_niveis, ajustes_manuais)
            s["_horario_atendimento_inicio"], s["_horario_atendimento_fim"] = \
                horario_efetivo(cnpj_destino, mapa_horarios, ajustes_manuais)
            # ajuste manual (Hugo, 22/08, via tela de Planejamento) já resolveu a
            # classificação -- não pendura mais o alerta de "precisa classificar
            # na planilha" pra um CNPJ que já foi corrigido manualmente
            doc_destino = "".join(c for c in (cnpj_destino or "") if c.isdigit())
            if nivel_requer_revisao and doc_destino not in ajustes_manuais:
                cnpjs_pendentes_nivel.add(cnpj_destino)

            tipo_carga, _ = classificar_tipo_carga(s.get("sender_id"), mapa_tipos_carga)
            s["_tipo_carga"] = tipo_carga
        _preparar_janelas(servicos, config)

        start_at = start_at_rota(data_alvo)  # HORA_INICIO_ROTA (06:00 BRT, Hugo 09/09)

        # Base pra seleção diária de modelo e sequenciamento (vizinho
        # mais proximo + 2-opt, Hugo 18/09). Geocodificada
        # também em modo teste (é cache hit, sem custo) porque a
        # seleção de modelo precisa da coordenada da base; se falhar,
        # cai pro fluxo antigo (agrupamento fixo + ordem de
        # proximidade, já razoável).
        coords_base = None
        try:
            coords_base = geocodificar(ENDERECO_BASE, gmaps_key)
        except Exception as e:
            logger.warning(f"Não consegui geocodificar a base -- seguindo com o agrupamento fixo: {e}")
        if coords_base:
            definir_coords_base(*coords_base)

        rotas_criadas = 0
        pedidos_alocados = 0
        indice_global = 1
        modelos_vencedores: dict[str, str] = {}
        rascunhos_acumulados: list[dict] = []

        def _rotear_particao(label: str, sublotes_do_dia: list[list[dict]]):
            nonlocal rotas_criadas, pedidos_alocados, indice_global, rotas_sem_motorista

            # Ordena por escassez de motorista ANTES de alocar (mais restrito
            # primeiro, pedido do Hugo, 20/08): sem isso, um motorista que
            # atende várias zonas pode ser "gasto" numa rota pequena de zona
            # flexível processada antes de uma rota grande de zona onde ele é
            # um dos poucos elegíveis (ver contar_motoristas_elegiveis em
            # alocacao_motoristas.py -- caso real 20/08, Zona Sul). Ordenação
            # estável: sublotes com a mesma contagem mantêm a ordem original
            # (a de saída do modelo vencedor, vizinho mais próximo desde
            # 18/09).
            sublotes_do_dia = sorted(
                sublotes_do_dia,
                key=lambda sub: contar_motoristas_elegiveis(
                    sub, data_alvo, catalogo_motoristas.motoristas, contagem_alocacoes_dia, gmaps_key,
                    ajustes_disponibilidade=ajustes_disponibilidade,
                ),
            )

            for sublote in sublotes_do_dia:
                nome_rota = f"{PREFIXO_NOME_ROTA} - {data_alvo_br} - #{indice_global}"
                indice_global += 1

                codigos = [s.get("code") for s in sublote]

                # Alocação de motorista (doc de alocação, seções 2.2/2.3):
                # classifica a rota como Viagem (>=1 entrega fora da
                # Grande SP) ou Grande SP, e escolhe o motorista elegível
                # com menor carga do dia. contagem_alocacoes_dia só é
                # incrementada DEPOIS de confirmar que a rota foi criada
                # de verdade (não conta alocação de rota que falhou).
                eh_viagem = classificar_rota_viagem(sublote, gmaps_key)
                tipo_rota_str = "VIAGEM" if eh_viagem else f"Grande SP/{classificar_rota_zona(sublote, gmaps_key) or '?'}"
                tipo_veiculo = classificar_tipo_veiculo(*caixas_e_enderecos(sublote))
                if tipo_veiculo:
                    tipo_rota_str += f" [veículo: {tipo_veiculo.nome}]"
                motorista = selecionar_motorista_equitativo(
                    sublote, data_alvo, catalogo_motoristas.motoristas, contagem_alocacoes_dia, gmaps_key,
                    ajustes_disponibilidade=ajustes_disponibilidade,
                )
                agent_id = motorista.agent_id if motorista else None
                vehicle_id = motorista.vehicle_id if motorista else None
                motorista_str = motorista.nome if motorista else "SEM MOTORISTA [ALERTA_ALOCACAO]"

                if gerar_rascunho:
                    zona = None if eh_viagem else classificar_rota_zona(sublote, gmaps_key)
                    km_estimado = (
                        calcular_km_estimado(sublote, coords_base[0], coords_base[1], gmaps_key)
                        if coords_base else None
                    )
                    horas_sublote = estimar_tempo_rota(sublote, gmaps_key, coords_base)
                    rascunhos_acumulados.append({
                        "nome": nome_rota,
                        "particao": rotulo_carga(sublote),
                        "tipo_rota": "VIAGEM" if eh_viagem else "GRANDE_SP",
                        "zona": zona,
                        "tipo_veiculo": tipo_veiculo.codigo if tipo_veiculo else None,
                        "agent_id": agent_id,
                        "vehicle_id": vehicle_id,
                        "motorista_nome": motorista.nome if motorista else None,
                        "start_location_base_id": BASE_LOCATION_ID,
                        "end_location_base_id": BASE_LOCATION_ID,
                        "start_at": start_at,
                        "km_estimado": km_estimado,
                        "horas_estimadas": round(horas_sublote, 2),
                        "sublote": sublote,
                    })
                    logger.info(f"[RASCUNHO] [{label}] '{nome_rota}' [{tipo_rota_str}] com {len(sublote)} pedido(s) "
                               f"(sequencia otimizada) -- motorista sugerido: {motorista_str}: {codigos}")
                    rotas_criadas += 1
                    pedidos_alocados += len(sublote)
                    if motorista:
                        contagem_alocacoes_dia[motorista.agent_id] = contagem_alocacoes_dia.get(motorista.agent_id, 0) + 1
                    else:
                        rotas_sem_motorista += 1
                    continue

                if modo_teste:
                    logger.info(f"[TESTE] [{label}] Criaria rota '{nome_rota}' [{tipo_rota_str}] com {len(sublote)} pedido(s) "
                               f"(sequencia otimizada) -- motorista: {motorista_str}: {codigos}")
                    rotas_criadas += 1
                    pedidos_alocados += len(sublote)
                    if motorista:
                        contagem_alocacoes_dia[motorista.agent_id] = contagem_alocacoes_dia.get(motorista.agent_id, 0) + 1
                    else:
                        rotas_sem_motorista += 1
                    continue

                try:
                    rota, sublote_criado, codigos_removidos = criar_rota_removendo_conflitos(
                        token, nome_rota, start_at, sublote,
                        start_location_base_id=BASE_LOCATION_ID, end_location_base_id=BASE_LOCATION_ID,
                        agent_id=agent_id, vehicle_id=vehicle_id,
                    )
                    if rota is None:
                        logger.error(f"'{nome_rota}': sobrou 0 pedido(s) depois de remover conflitos "
                                    f"({codigos_removidos}) -- nenhuma rota criada pra esse lote.")
                        continue

                    codigos_finais = [s.get("code") for s in sublote_criado]
                    aviso_removidos = f" (removidos por conflito: {codigos_removidos})" if codigos_removidos else ""
                    logger.info(f"Rota criada: '{nome_rota}' (id={rota['id']}) [{tipo_rota_str}] com {len(sublote_criado)} "
                               f"pedido(s) (sequencia otimizada){aviso_removidos} -- "
                               f"motorista: {motorista_str}: {codigos_finais}")
                    for s in sublote_criado:
                        marcar_alocado(s["id"], rota["id"])
                    # Romaneio da rota preparado em segundo plano (Hugo,
                    # 09/09) -- main() espera a fila esvaziar antes de sair.
                    agendar_documentacao(rota["id"], data_alvo, motivo=f"rota criada ({nome_rota})")
                    rotas_criadas += 1
                    pedidos_alocados += len(sublote_criado)
                    if motorista:
                        contagem_alocacoes_dia[motorista.agent_id] = contagem_alocacoes_dia.get(motorista.agent_id, 0) + 1
                    else:
                        rotas_sem_motorista += 1
                except Exception as e:
                    logger.error(f"Falha ao criar rota '{nome_rota}': {e}")

        planos = planejar_sublotes(servicos, coords_base, gmaps_key, data_alvo)
        for plano in planos:
            logger.info(f"Partição '{plano['label']}': modelo {plano['modelo']}, {len(plano['sublotes'])} rota(s).")
            modelos_vencedores[plano["label"]] = plano["modelo"]
            _rotear_particao(plano["label"], plano["sublotes"])

        if gerar_rascunho and rascunhos_acumulados:
            lote_id = criar_lote_rascunhos(data_alvo, rascunhos_acumulados)
            logger.info(f"Lote de rascunhos gravado: '{lote_id}' ({len(rascunhos_acumulados)} rascunho(s)) "
                       f"-- aguardando revisão no painel de planejamento.")

        prefixo_teste = "[Teste] " if modo_teste else ""
        if gerar_rascunho:
            detalhe_criacao = (f"{rotas_criadas} rascunho(s) gerado(s), {pedidos_alocados} pedido(s) alocado(s), "
                               f"aguardando revisão no painel de planejamento.")
        else:
            detalhe_criacao = f"{prefixo_teste}{rotas_criadas} rota(s) criada(s), {pedidos_alocados} pedido(s) alocado(s)."
        resumo_etapas["Criação de rotas"] = {
            "status": "ok",
            "detalhe": detalhe_criacao
                      + (" Modelo do dia: " + "; ".join(f"{l}: {m}" for l, m in modelos_vencedores.items()) + "."
                         if modelos_vencedores else "")
                      + (f" [ALERTA_NIVEL] {len(cnpjs_pendentes_nivel)} CNPJ(s) de destinatário sem "
                         f"classificação de nível na planilha de complexidade — nível padrão (2) "
                         f"aplicado provisoriamente, requer classificação manual."
                         if cnpjs_pendentes_nivel else ""),
        }

        matriz_alocacao = ", ".join(
            f"{next((m.nome for m in catalogo_motoristas.motoristas if m.agent_id == agent_id), agent_id)}: {qtd}"
            for agent_id, qtd in sorted(contagem_alocacoes_dia.items(), key=lambda item: -item[1])
        ) or "nenhuma"
        resumo_etapas["Alocação de motoristas"] = {
            "status": "ok" if rotas_sem_motorista == 0 else "erro",
            "detalhe": f"{prefixo_teste}Distribuição: {matriz_alocacao}."
                      + (f" [ALERTA_ALOCACAO] {rotas_sem_motorista} rota(s) sem motorista disponível."
                         if rotas_sem_motorista else ""),
        }

    except Exception as e:
        logger.exception(f"Erro na criação de rotas diárias: {e}")
        resumo_etapas["Criação de rotas"] = {"status": "erro", "detalhe": str(e)}

    # Romaneios das rotas recém-criadas (documentacao_rota) terminam
    # antes de o processo encerrar -- sem isso a thread morreria junto.
    aguardar_documentacao()

    duracao = time.time() - inicio
    logger.info(f"Criação de rotas diárias finalizada em {duracao:.1f}s.")

    try:
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cria as rotas do dia seguinte a partir dos pedidos not_assigned")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Mostra o que seria criado, sem chamar a API de verdade")
    parser.add_argument("--gerar-rascunho", action="store_true",
                        help="Grava os sublotes calculados como rascunho local (dados/dados.db) "
                             "para revisão/ajuste no painel de planejamento, em vez de criar a rota "
                             "direto na VUUPT")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste, gerar_rascunho=args.gerar_rascunho)
