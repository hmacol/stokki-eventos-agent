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
criar a rota, os pedidos são ordenados da mais LONGE pra mais PERTO
da base (roteirizacao_dados.py::ordenar_por_distancia_base) -- padrão
de sequenciamento pedido pelo Hugo, 03/08, que substituiu a abordagem
anterior (chamar o solver de route-optimization só pra sequenciar,
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

from roteirizacao_dados import agrupar_por_regiao, consolidar_regioes_pequenas, dividir_em_sublotes, elegivel_para_data, calcular_km_estimado
from selecao_modelo import escolher_melhor_modelo
from rotas_client import criar_rota_removendo_conflitos
from fingerprint_rotas import marcar_alocado
sys.path.insert(0, str(_RAIZ_PROJETO / "painel_agentes"))
from rascunhos_rota import criar_lote_rascunhos
from notificar_agendamento_pendente import identificar_pendentes, notificar_remetentes
from regras.clientes_agendamento import carregar_clientes_agendamento, tem_agendamento
from agendamento_confirmacao import buscar_confirmacao
from regioes_dia_fixo import aplicar_regioes_dia_fixo
from notificar_agendamento_dia_fixo import notificar_agendamentos_dia_fixo
from notificar_area_nao_atendida import identificar_area_nao_atendida, notificar_remetentes as notificar_area_nao_atendida
from regras.preferencias_motoristas import CatalogoMotoristas
from regras.complexidade_entrega import carregar_niveis, classificar_nivel
from regras.tipo_carga_embarcador import carregar_tipos_carga_por_sender, classificar_tipo_carga, TIPOS_CARGA_FRIA
from alocacao_motoristas import classificar_rota_viagem, selecionar_motorista_equitativo
from zonas_sp import classificar_rota_zona

ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"
BASE_LOCATION_ID = 6950  # confirmado em produção (operational_base_id da base, visto em dados reais do VUUPT)
DB_PATH = _RAIZ_PROJETO / "dados" / "dados.db"
TAMANHO_MINIMO_ROTA = 10
TAMANHO_MAXIMO_ROTA = 18  # Aumentado de 15 para 18 entregas por rota (pedido do Hugo, 09/08)
VOLUME_MAXIMO_ROTA = 100  # Novo limite máximo de caixas/volumes por rota (pedido do Hugo, 09/08)
DISTANCIA_MAXIMA_ROTA_KM = 20  # Máximo entre pedidos da mesma rota DENTRO da Grande SP (pedido do Hugo, 09/08 -- ajustado 10/08)
# Rotas de Viagem (fora da Grande SP) NÃO têm limite de distância entre
# pedidos (pedido do Hugo, 10/08): as próprias regiões de dia fixo já
# têm vãos internos maiores que 15/20km (ex: Vale do Paraíba chega a
# ~55km entre Suzano e São José dos Campos) -- aplicar o mesmo limite
# urbano lá só fracionava a região inteira em várias rotas pequenas
# sem necessidade, já que é deslocamento longo de qualquer forma.
DISTANCIA_MAXIMA_VIAGEM_KM = None
PREFIXO_NOME_ROTA = "Planejamento"


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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
        data_alvo_str = data_alvo.strftime("%Y-%m-%d")
        data_alvo_br  = data_alvo.strftime("%d/%m/%Y")  # formato usado no NOME da rota (convenção nativa do VUUPT)
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
                resultado_area = notificar_area_nao_atendida(pendentes_area, config.get("email", {}), modo_teste=modo_teste)
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
            if pendentes:
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
        mapa_tipos_carga = carregar_tipos_carga_por_sender(DB_PATH)
        cnpjs_pendentes_nivel = set()
        for s in servicos:
            cnpj_destino = (s.get("customer") or {}).get("code", "")
            nivel, _, nivel_requer_revisao = classificar_nivel(cnpj_destino, mapa_niveis)
            s["_nivel_dificuldade"] = nivel
            if nivel_requer_revisao:
                cnpjs_pendentes_nivel.add(cnpj_destino)

            tipo_carga, _ = classificar_tipo_carga(s.get("sender_id"), mapa_tipos_carga)
            s["_tipo_carga"] = tipo_carga

        # Partição por tipo de carga (pedido do Hugo, 10/08: "as entregas
        # Secas deveriam ser roteirizadas separadas das refrigeradas e
        # congeladas") -- Seco de um lado, Refrigerado+Congelado do outro
        # (esses dois JUNTOS entre si, só separados de Seco), cada partição
        # passando pelo MESMO fluxo de agrupamento geográfico/divisão em
        # sublotes de forma independente, então nenhuma rota mistura os dois
        # grupos.
        particoes = [
            ("Seco", [s for s in servicos if s["_tipo_carga"] not in TIPOS_CARGA_FRIA]),
            ("Refrigerado/Congelado", [s for s in servicos if s["_tipo_carga"] in TIPOS_CARGA_FRIA]),
        ]
        for label, servicos_particao in particoes:
            logger.info(f"Partição '{label}': {len(servicos_particao)} pedido(s).")

        start_at = f"{data_alvo_str}T13:00:00Z"

        # Base pra seleção diária de modelo e sequenciamento (mais
        # LONGE -> mais PERTO, pedido do Hugo, 03/08). Geocodificada
        # também em modo teste (é cache hit, sem custo) porque a
        # seleção de modelo precisa da coordenada da base; se falhar,
        # cai pro fluxo antigo (agrupamento fixo + ordem de
        # proximidade, já razoável).
        coords_base = None
        try:
            coords_base = geocodificar(ENDERECO_BASE, gmaps_key)
        except Exception as e:
            logger.warning(f"Não consegui geocodificar a base -- seguindo com o agrupamento fixo: {e}")

        rotas_criadas = 0
        pedidos_alocados = 0
        indice_global = 1
        modelos_vencedores: dict[str, str] = {}
        rascunhos_acumulados: list[dict] = []

        def _rotear_particao(servicos_particao: list[dict], label: str):
            nonlocal rotas_criadas, pedidos_alocados, indice_global, rotas_sem_motorista

            if not servicos_particao:
                return

            # Seleção diária de modelo (pedido do Hugo, 10/08): compara
            # Atual x Sweep x Clarke-Wright sobre os pedidos DO DIA
            # (todos sequenciados com 2-opt, 1ª entrega sempre a mais
            # distante) e libera as rotas com o vencedor -- menos
            # rotas primeiro, menor KM como desempate. Sem coordenada
            # da base não dá pra comparar: cai pro fluxo fixo antigo.
            if coords_base:
                modelo_vencedor, sublotes_do_dia = escolher_melhor_modelo(
                    servicos_particao, coords_base[0], coords_base[1], gmaps_key,
                    data_alvo=data_alvo, label=label,
                    tamanho_minimo=TAMANHO_MINIMO_ROTA, tamanho_maximo=TAMANHO_MAXIMO_ROTA,
                    volume_maximo=VOLUME_MAXIMO_ROTA,
                    distancia_maxima_km=DISTANCIA_MAXIMA_ROTA_KM,
                    distancia_maxima_viagem_km=DISTANCIA_MAXIMA_VIAGEM_KM,
                )
                modelos_vencedores[label] = modelo_vencedor
            else:
                sublotes_do_dia = []
                grupos_iniciais = agrupar_por_regiao(servicos_particao, api_key=gmaps_key)
                logger.info(f"[{label}] {len(grupos_iniciais)} região(ões) geográfica(s) inicial(is).")
                grupos_validos = consolidar_regioes_pequenas(grupos_iniciais, minimo=TAMANHO_MINIMO_ROTA, api_key=gmaps_key)
                logger.info(f"[{label}] Após consolidar regiões pequenas: {len(grupos_validos)} região(ões) final(is).")

                for servicos_regiao in grupos_validos.values():
                    # A trava de distância só vale DENTRO da Grande SP --
                    # região de Viagem (>=1 entrega fora da Grande SP) não
                    # tem limite (ver DISTANCIA_MAXIMA_VIAGEM_KM acima).
                    distancia_maxima_da_regiao = (
                        DISTANCIA_MAXIMA_VIAGEM_KM if classificar_rota_viagem(servicos_regiao, gmaps_key)
                        else DISTANCIA_MAXIMA_ROTA_KM
                    )
                    sublotes_do_dia.extend(dividir_em_sublotes(
                        servicos_regiao, tamanho_minimo=TAMANHO_MINIMO_ROTA,
                        tamanho_maximo=TAMANHO_MAXIMO_ROTA, volume_maximo=VOLUME_MAXIMO_ROTA,
                        distancia_maxima_km=distancia_maxima_da_regiao, api_key=gmaps_key))

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
                motorista = selecionar_motorista_equitativo(
                    sublote, data_alvo, catalogo_motoristas.motoristas, contagem_alocacoes_dia, gmaps_key,
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
                    rascunhos_acumulados.append({
                        "nome": nome_rota,
                        "particao": label,
                        "tipo_rota": "VIAGEM" if eh_viagem else "GRANDE_SP",
                        "zona": zona,
                        "agent_id": agent_id,
                        "vehicle_id": vehicle_id,
                        "motorista_nome": motorista.nome if motorista else None,
                        "start_location_base_id": BASE_LOCATION_ID,
                        "end_location_base_id": BASE_LOCATION_ID,
                        "start_at": start_at,
                        "km_estimado": km_estimado,
                        "sublote": sublote,
                    })
                    logger.info(f"[RASCUNHO] [{label}] '{nome_rota}' [{tipo_rota_str}] com {len(sublote)} pedido(s) "
                               f"(mais longe -> mais perto da base) -- motorista sugerido: {motorista_str}: {codigos}")
                    rotas_criadas += 1
                    pedidos_alocados += len(sublote)
                    if motorista:
                        contagem_alocacoes_dia[motorista.agent_id] = contagem_alocacoes_dia.get(motorista.agent_id, 0) + 1
                    else:
                        rotas_sem_motorista += 1
                    continue

                if modo_teste:
                    logger.info(f"[TESTE] [{label}] Criaria rota '{nome_rota}' [{tipo_rota_str}] com {len(sublote)} pedido(s) "
                               f"(mais longe -> mais perto da base) -- motorista: {motorista_str}: {codigos}")
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
                               f"pedido(s) (mais longe -> mais perto da base){aviso_removidos} -- "
                               f"motorista: {motorista_str}: {codigos_finais}")
                    for s in sublote_criado:
                        marcar_alocado(s["id"], rota["id"])
                    rotas_criadas += 1
                    pedidos_alocados += len(sublote_criado)
                    if motorista:
                        contagem_alocacoes_dia[motorista.agent_id] = contagem_alocacoes_dia.get(motorista.agent_id, 0) + 1
                    else:
                        rotas_sem_motorista += 1
                except Exception as e:
                    logger.error(f"Falha ao criar rota '{nome_rota}': {e}")

        for label, servicos_particao in particoes:
            _rotear_particao(servicos_particao, label)

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
