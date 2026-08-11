# -*- coding: utf-8 -*-
"""
incrementar_rotas.py

Job de hora em hora, das 14h às 20h (pedido do Hugo, 01/08): pega
pedidos not_assigned NOVOS e aloca cada um na rota de HOJE mais
PRÓXIMA geograficamente que ainda tenha espaço (< 15 pedidos) --
usando POST /routes/{id}/activities com position="end" (achado em
produção, 06/08: "optimize" exige veículo atribuído na rota, que
essas rotas nunca têm -- a ordem final não importa de qualquer
forma, o bloco de resequenciamento mais abaixo reordena tudo por
distância da base depois).

"Novo" é decidido CONTRA A API, não por um fingerprint local (mudado
06/08, pedido do Hugo): busca todas as rotas existentes com
include=services e monta o conjunto de todo service_id que já
aparece em alguma delas -- um pedido not_assigned só é considerado
"novo" se NÃO estiver nesse conjunto. Isso evita o problema do
fingerprint local ficar desatualizado quando o Hugo ajusta rotas na
mão (ex: remove um pedido de uma rota -- com fingerprint, esse
pedido ficava travado pra sempre achando que já tinha sido alocado,
mesmo não estando em rota nenhuma de verdade).

Se a rota mais próxima já estiver cheia, cria uma rota NOVA na mesma
região (pedido do Hugo) -- essa rota nova pode crescer nos próximos
incrementos, à medida que mais pedidos da região forem chegando.

Rotas de "hoje" são identificadas pelo nome (mesmo prefixo e data que
criar_rotas_diarias.py usa) -- não por um ID salvo em algum lugar, pra
funcionar mesmo que os dois scripts rodem em processos/dias diferentes.

ATENÇÃO: a forma de calcular o centroide de uma rota já existente
(buscar os serviços dela via include=services) ainda não foi validada
contra um retorno real da API -- a documentação não mostra um exemplo
completo dessa resposta. Recomendo rodar --modo-teste primeiro e
conferir se os campos batem antes de confiar isso em produção.

COMO USAR:
    py -3.11 incrementar_rotas.py                # execução normal
    py -3.11 incrementar_rotas.py --modo-teste    # só mostra o que faria
"""
import argparse
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "incrementar_rotas.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("incrementar_rotas")

import yaml

from vuupt_client import VuuptClient
from geocodificacao import geocodificar
from notificar_execucao_agente import notificar_execucao

from roteirizacao_dados import obter_coordenadas, elegivel_para_data, extrair_volume_caixas, _distancia_km, agrupar_por_regiao, consolidar_regioes_pequenas, dividir_em_sublotes, ordenar_por_distancia_base
from rotas_client import listar_rotas, criar_rota, adicionar_atividades, atualizar_rota
from criar_rotas_diarias import (
    BASE_LOCATION_ID, ENDERECO_BASE, PREFIXO_NOME_ROTA, TAMANHO_MINIMO_ROTA, TAMANHO_MAXIMO_ROTA, VOLUME_MAXIMO_ROTA,
    DISTANCIA_MAXIMA_ROTA_KM, DISTANCIA_MAXIMA_VIAGEM_KM, TZ_BRASILIA, _data_alvo_rotas,
)
from notificar_agendamento_pendente import identificar_pendentes
from regras.clientes_agendamento import carregar_clientes_agendamento, tem_agendamento
from agendamento_confirmacao import buscar_confirmacao
from regioes_dia_fixo import aplicar_regioes_dia_fixo
from notificar_area_nao_atendida import identificar_area_nao_atendida, notificar_remetentes as notificar_area_nao_atendida
from regras.preferencias_motoristas import CatalogoMotoristas
from alocacao_motoristas import classificar_rota_viagem, selecionar_motorista_equitativo
from zonas_sp import classificar_rota_zona, classificar_zona


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


PADRAO_DATA_ROTA = re.compile(r"(\d{2})/(\d{2})/(\d{4})")

# Achado em produção, 06/08: rota com data de amanhã (passa pela trava de
# segurança de data numa boa) mas com STATUS "Cancelada" no VUUPT (ex:
# cancelada manualmente) -- o POST /activities recusa com 400 "A rota não
# pode ser alterada no status atual". Detecta esse padrão especificamente
# pra remover só a rota problemática das candidatas e tentar outra, sem
# perder o pedido (ver main()).
PADRAO_ROTA_INVALIDA = re.compile(r"n[ãa]o pode ser alterada no status atual", re.IGNORECASE)
PADRAO_DATA_START_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _data_da_rota_e_passada(nome_rota: str) -> bool:
    """
    Extrai a data DD/MM/AAAA do nome da rota (convenção
    'Planejamento - DD/MM/AAAA - #N') e diz se é HOJE ou ANTERIOR --
    usado como trava de segurança extra (ver comentário no main()),
    pra nunca mexer numa rota que já é de hoje ou já passou, não
    importa como o nome dela apareceu na lista.
    """
    match = PADRAO_DATA_ROTA.search(nome_rota)
    if not match:
        return False  # nome sem data reconhecível -- não temos como avaliar, não bloqueia por essa via
    dia, mes, ano = match.groups()
    try:
        data_rota = date(int(ano), int(mes), int(dia))
    except ValueError:
        return False
    return data_rota <= date.today()


def _data_inicio_rota(rota: dict) -> date | None:
    """
    Data de início REAL da rota, direto do campo start_at que a
    própria API devolve (pedido do Hugo, 06/08 -- mais confiável que
    extrair do nome, que é só texto de exibição e teoricamente
    poderia ser editado sem mudar a data de verdade). Aceita tanto
    'AAAA-MM-DDTHH:MM:SSZ' (formato que a gente ENVIA ao criar) quanto
    'AAAA-MM-DD HH:MM:SS' (formato que a API às vezes DEVOLVE em
    outros campos do projeto) -- extrai só a parte AAAA-MM-DD, com
    regex tolerante ao separador. Retorna None se a rota não tiver
    start_at ou vier num formato irreconhecível (nesse caso, quem
    chama decide como agir -- não assume nada).
    """
    start_at = rota.get("start_at")
    if not start_at:
        return None
    match = PADRAO_DATA_START_AT.search(str(start_at))
    if not match:
        return None
    ano, mes, dia = match.groups()
    try:
        return date(int(ano), int(mes), int(dia))
    except ValueError:
        return None


# Status de rota que ainda não estão na rua (confirmados contra dado real
# da API, 11/08): "not_started", "assigned", "accepted" e "not_assigned"
# -- pedido do Hugo, permitir incrementar rota de HOJE nesses casos, já
# que o motorista ainda não começou a rodar. "started" continua bloqueado
# (rota em execução -- é exatamente o caso que motivou a trava original,
# 06/08: motorista reclamou de pedido aparecendo do nada numa rota já na
# rua). Qualquer status não reconhecido (novo/desconhecido) também
# bloqueia, por segurança -- só libera pros valores explicitamente
# confirmados como seguros.
STATUS_ROTA_HOJE_LIBERADOS = {"not_started", "assigned", "accepted", "not_assigned"}


def _rota_e_de_hoje_ou_passada(rota: dict) -> bool:
    """
    Trava de segurança principal (06/08, pedido do Hugo -- crítico:
    motorista reclamou de pedido aparecendo do nada numa rota já na
    rua; ajustada 11/08, também pedido do Hugo, pra liberar rota de
    HOJE quando o status ainda indica que ela não saiu pra rua -- ver
    STATUS_ROTA_HOJE_LIBERADOS). Prioriza o campo start_at REAL da
    API; só cai pro nome como reserva se start_at não vier preenchido
    ou não for reconhecível -- nunca o contrário, pra não confiar em
    texto de exibição quando temos o dado estruturado disponível.
    Rota de data PASSADA (anterior a hoje) continua sempre bloqueada,
    não importa o status.
    """
    data_start_at = _data_inicio_rota(rota)
    if data_start_at is not None:
        if data_start_at < date.today():
            return True
        if data_start_at == date.today():
            return rota.get("status") not in STATUS_ROTA_HOJE_LIBERADOS
        return False
    return _data_da_rota_e_passada(rota.get("name", ""))


def _extrair_servicos_da_rota(rota: dict) -> list[dict]:
    """
    Extrai a lista de serviços de uma rota vinda com include=services.
    CONFIRMADO com dado real (02/08): o campo 'services' vem
    EMBRULHADO como {"data": [...]} -- mesmo padrão de paginação
    usado no resto da API VUUPT, não uma lista direta como se
    imaginava antes de testar contra resposta real.
    """
    servicos_wrapper = rota.get("services")
    if isinstance(servicos_wrapper, dict):
        return servicos_wrapper.get("data", []) or []
    if isinstance(servicos_wrapper, list):
        return servicos_wrapper
    return []


def _centroide_rota(rota: dict, api_key: str | None) -> tuple[float, float] | None:
    """
    Centroide de coordenadas dos serviços já na rota (rota veio com
    include=services). Cada serviço já vem com latitude/longitude
    prontos (o próprio VUUPT já geocodificou na importação) -- usa
    direto, sem gastar consulta ao cache/Google de novo; só cai pra
    obter_coordenadas() como reserva se, por algum motivo, latitude/
    longitude não vierem preenchidos num serviço específico. Retorna
    None se não conseguir calcular (sem serviços, ou nenhum com
    coordenada disponível de nenhuma das duas formas).
    """
    servicos = _extrair_servicos_da_rota(rota)
    coords_lista = []
    for s in servicos:
        if not isinstance(s, dict):
            continue
        lat, lng = s.get("latitude"), s.get("longitude")
        if lat and lng:
            try:
                coords_lista.append((float(lat), float(lng)))
                continue
            except (TypeError, ValueError):
                pass
        c = obter_coordenadas(s, api_key)
        if c:
            coords_lista.append(c)

    if not coords_lista:
        return None
    return (
        sum(c[0] for c in coords_lista) / len(coords_lista),
        sum(c[1] for c in coords_lista) / len(coords_lista),
    )


def _proximo_indice_disponivel(rotas_hoje: list[dict]) -> int:
    """
    Acha o próximo índice global disponível pro nome da rota (formato
    'Planejamento - DD/MM/AAAA - #N', mesma convenção nativa do VUUPT,
    numeração GLOBAL pro dia -- não mais por região). Extrai o maior
    #N já usado hoje e soma 1; começa em 1 se não houver nenhuma rota
    ainda.
    """
    maior = 0
    for r in rotas_hoje:
        match = re.search(r"#(\d+)\s*$", r.get("name", ""))
        if match:
            maior = max(maior, int(match.group(1)))
    return maior + 1


def main(modo_teste: bool = False):
    inicio = time.time()
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Incremento de rotas iniciado.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    cfg_motoristas = config.get("motoristas", {})
    catalogo_motoristas = CatalogoMotoristas.carregar(
        cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""),
    )
    motoristas_por_id = {m.agent_id: m for m in catalogo_motoristas.motoristas}
    rotas_sem_motorista = 0

    resumo_etapas = {}

    try:
        vuupt = VuuptClient(token)
        # Mesma regra de data alvo do criar_rotas_diarias (pedido do
        # Hugo, 10/08 -- incrementos passaram a rodar das 19h às 00h,
        # DEPOIS da criação das rotas às 18h): antes das 14h o alvo é
        # o próprio dia (cobre a rodada da meia-noite, que incrementa
        # as rotas do dia que acabou de começar), a partir das 14h é o
        # próximo dia útil (mesmas rotas que a criação das 18h gerou).
        # Substitui o "date.today() + 1 dia" fixo, que à meia-noite
        # apontava pro dia SEGUINTE ao das rotas recém-criadas, e na
        # sexta apontava pro sábado (sem rota nenhuma).
        data_alvo = _data_alvo_rotas(datetime.now(TZ_BRASILIA))
        data_alvo_str = data_alvo.strftime("%Y-%m-%d")
        data_alvo_br  = data_alvo.strftime("%d/%m/%Y")  # formato usado no NOME da rota (convenção nativa do VUUPT)

        filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
        servicos = vuupt.listar_servicos(filtro, per_page=100)

        # Busca as rotas ATIVAS (hoje em diante) -- pedido do Hugo, 06/08:
        # antes buscava o HISTÓRICO INTEIRO de rotas (rotas de anos atrás,
        # já finalizadas, sem necessidade nenhuma de olhar pra elas). O
        # filtro por start_at só passou a funcionar de verdade agora (ver
        # rotas_client.py -- o parâmetro "filtro" tinha um bug de
        # serialização que fazia a API ignorar o filtro e devolver tudo,
        # mesmo bug que afetava mapa_rotas.py). Fallback pro histórico
        # completo se o filtro falhar por algum motivo (nunca finge que
        # não há rota nenhuma só porque o filtro deu erro).
        #
        # Usado tanto pra achar "rota de amanhã com espaço" quanto pra
        # saber se um pedido "já está em alguma rota" -- em ambos os
        # casos só rotas de HOJE em diante interessam; nenhuma rota já
        # finalizada (passada) precisa entrar nessa conta.
        hoje_str = date.today().strftime("%Y-%m-%d") + " 00:00:00"
        filtro_ativas = [{"field": "start_at", "operator": "gte", "value": hoje_str}]
        try:
            todas_rotas = listar_rotas(token, include=["services"], filtro=filtro_ativas)
            logger.info(f"{len(todas_rotas)} rota(s) ativa(s) (hoje em diante) encontrada(s) via filtro.")
        except Exception as e:
            logger.warning(f"Filtro por start_at falhou ({e}) -- caindo pro histórico completo (mais lento).")
            todas_rotas = listar_rotas(token, include=["services"])

        # TRAVA DE STATUS (06/08, achado com o Hugo investigando discrepância
        # real: PS-35431/PS-35440 apareciam como "já em rota" pra API, mas o
        # VUUPT não mostrava eles em rota nenhuma) -- a causa era a rota
        # 5050669 estar com status "canceled". start_at e status são campos
        # INDEPENDENTES -- uma rota cancelada pode perfeitamente ter start_at
        # de amanhã e passar pela trava de data numa boa. Filtra ANTES de
        # qualquer outro uso (tanto pra "já em rota" quanto pra candidatas de
        # alocação) -- rota cancelada não deveria contar pra nada disso, os
        # serviços que sobraram nela são, na prática, órfãos disponíveis.
        rotas_canceladas = [r for r in todas_rotas if r.get("status") == "canceled"]
        if rotas_canceladas:
            logger.warning(f"{len(rotas_canceladas)} rota(s) com status 'canceled' EXCLUÍDA(S) -- "
                           f"não contam como 'já em rota' nem como candidatas: "
                           f"{[(r.get('name'), r.get('id')) for r in rotas_canceladas]}")
        todas_rotas = [r for r in todas_rotas if r.get("status") != "canceled"]

        # Em vez de confiar no fingerprint local pra saber se um pedido
        # "já foi alocado" (que fica desatualizado quando o Hugo mexe
        # manualmente numa rota -- removendo um pedido, por exemplo, o
        # fingerprint continuava achando que ele tava alocado e nunca
        # mais roteirizava de novo), checa DIRETO na API: esse pedido
        # not_assigned está de fato em alguma rota agora? Se sim, pula
        # (já foi cuidado, seja pelo agente ou manualmente). Se não, é
        # elegível -- sempre respeitando o agendamento (ver mais abaixo).
        ids_ja_em_alguma_rota: set[int] = set()
        for r in todas_rotas:
            for s in _extrair_servicos_da_rota(r):
                if isinstance(s, dict) and s.get("id") is not None:
                    ids_ja_em_alguma_rota.add(s["id"])

        # Regiões com dia fixo de entrega (pedido do Hugo, 02/08) --
        # roda antes de tudo, mesma lógica de criar_rotas_diarias.py.
        try:
            qtd_agendados_dia_fixo = aplicar_regioes_dia_fixo(servicos, vuupt)
            if qtd_agendados_dia_fixo:
                logger.info(f"{qtd_agendados_dia_fixo} pedido(s) agendado(s) por região de dia fixo.")
        except Exception as e:
            logger.error(f"Falha ao aplicar regiões de dia fixo (não afeta o incremento): {e}")

        # Área não atendida (pedido do Hugo, 02/08) -- diferente do
        # agendamento pendente, esse aviso é ÚNICO (não é rate-limitado
        # por dia) e pode surgir a qualquer hora, então notifica aqui
        # também (não só no job das 13h) pra quem aparecer novo.
        ids_area_nao_atendida = set()
        try:
            pendentes_area = identificar_area_nao_atendida(servicos, gmaps_key)
            ids_area_nao_atendida = {s["id"] for s, _tipo in pendentes_area}
            if pendentes_area:
                resultado_area = notificar_area_nao_atendida(pendentes_area, config.get("email", {}), modo_teste=modo_teste)
                logger.info(f"Notificação de área não atendida: {resultado_area}")
        except Exception as e:
            logger.error(f"Falha ao notificar área não atendida (não afeta o incremento): {e}")

        # Não aloca pedido que exige agendamento e está com ele em
        # branco/vencido sem resposta ainda (pedido do Hugo, 02/08:
        # "aguardar resposta do e-mail") -- criar_rotas_diarias.py já
        # cuida de notificar o remetente 1x/dia, aqui só filtramos.
        ids_pendentes_agendamento = set()
        try:
            caminho_agendamento = config.get("clientes_agendamento", {}).get("planilha", "")
            conjunto_agendamento = carregar_clientes_agendamento(caminho_agendamento)
            pendentes = identificar_pendentes(servicos, conjunto_agendamento, vuupt, tem_agendamento,
                                             buscar_confirmacao_fn=buscar_confirmacao)
            ids_pendentes_agendamento = {s["id"] for s in pendentes}
        except Exception as e:
            logger.error(f"Falha ao checar agendamento pendente (não afeta o incremento): {e}")

        # não_elegivel_data (agendamento com data futura) contado à parte
        # -- é o único filtro que ainda não tinha um "ids_..." próprio.
        ids_nao_elegivel_data = {s["id"] for s in servicos if not elegivel_para_data(s, data_alvo)}

        # BUG CORRIGIDO (06/08, achado no log real do Hugo -- "já em alguma
        # rota ativa: 197" sendo MAIOR que o total de 76 not_assigned, óbvio
        # sinal de erro): `ids_ja_em_alguma_rota` é o conjunto de TODOS os
        # serviços em TODAS as rotas ativas (pode ser bem maior que o total
        # not_assigned, a maioria nem faz parte dele) -- pra reportar
        # corretamente "quantos dos not_assigned atuais já estão em rota",
        # precisa da INTERSEÇÃO com os service_ids dos `servicos` de agora,
        # não o tamanho bruto do conjunto inteiro.
        ids_servicos_atuais = {s["id"] for s in servicos}
        ids_ja_em_rota_deste_lote = ids_ja_em_alguma_rota & ids_servicos_atuais

        novos = [
            s for s in servicos
            if s["id"] not in ids_ja_em_alguma_rota and elegivel_para_data(s, data_alvo)
            and s["id"] not in ids_pendentes_agendamento
            and s["id"] not in ids_area_nao_atendida
        ]

        # Detalhamento completo do que aconteceu com CADA pedido not_assigned
        # (pedido do Hugo, 06/08: "só 36 de 76 foram roteirizados, quero
        # entender o motivo") -- antes só logava o total elegível, sem
        # dizer POR QUE os outros ficaram de fora. Um pedido pode cair em
        # mais de uma categoria (ex: já em rota E com agendamento pendente)
        # -- a soma das categorias pode passar do total de excluídos por
        # causa disso, não é bug.
        excluidos = len(servicos) - len(novos)
        if excluidos:
            logger.info(
                f"{excluidos} pedido(s) not_assigned NÃO entraram nesse incremento -- motivos "
                f"(um pedido pode contar em mais de uma categoria): "
                f"já em alguma rota ativa: {len(ids_ja_em_rota_deste_lote)} | "
                f"agendamento com data futura: {len(ids_nao_elegivel_data)} | "
                f"aguardando confirmação de agendamento: {len(ids_pendentes_agendamento)} | "
                f"área não atendida/fora de SP: {len(ids_area_nao_atendida)}."
            )
        logger.info(f"{len(novos)} pedido(s) novo(s) e elegível(is) pra alocar (de {len(servicos)} not_assigned no total, "
                   f"{len(ids_ja_em_rota_deste_lote)} já em alguma rota).")

        if not novos:
            resumo_etapas["Incremento de rotas"] = {"status": "ok", "detalhe": "Nenhum pedido novo pra alocar."}
            return

        prefixo_hoje = f"{PREFIXO_NOME_ROTA} - {data_alvo_br}"

        # Base pra ordenar rotas da mais LONGE pra mais PERTO (pedido
        # do Hugo, 03/08 -- padrão de sequenciamento). Se falhar, as
        # rotas seguem com a ordem que já tinham/proximidade original.
        coords_base = None
        try:
            coords_base = geocodificar(ENDERECO_BASE, gmaps_key)
        except Exception as e:
            logger.warning(f"Não consegui geocodificar a base pra ordenar as rotas: {e}")

        rotas_hoje = [r for r in todas_rotas if r.get("name", "").startswith(prefixo_hoje)]

        # TRAVA DE SEGURANÇA (06/08, pedido do Hugo -- crítico): motorista
        # reclamou de pedido aparecendo do nada numa rota que já estava
        # na rua. Ajustada 11/08 (também pedido do Hugo): rota de HOJE com
        # status "not_started", "assigned" ou "accepted" -- motorista ainda
        # não começou a rodar -- passa a ser candidata normalmente; só
        # continua bloqueada rota de hoje com status "started" (ou
        # qualquer outro não reconhecido) e QUALQUER rota de data passada,
        # não importa o status (ver STATUS_ROTA_HOJE_LIBERADOS e
        # _rota_e_de_hoje_ou_passada). Checa a data/status REAIS da API
        # (mais confiável que o nome, que é só texto de exibição).
        rotas_bloqueadas = [r for r in todas_rotas if _rota_e_de_hoje_ou_passada(r)]
        if rotas_bloqueadas:
            logger.warning(f"{len(rotas_bloqueadas)} rota(s) de hoje (em execução/status não liberado) ou "
                           f"passada(s) encontrada(s) e EXCLUÍDA(S) por segurança (nunca alocamos nelas): "
                           f"{[(r.get('name'), r.get('start_at'), r.get('status')) for r in rotas_bloqueadas]}")
        ids_bloqueados = {r["id"] for r in rotas_bloqueadas}
        rotas_hoje = [r for r in rotas_hoje if r["id"] not in ids_bloqueados]

        logger.info(f"{len(rotas_hoje)} rota(s) de hoje encontrada(s).")

        info_rotas = []
        todos_servicos_por_id: dict[int, dict] = {}
        for r in rotas_hoje:
            servicos_rota = _extrair_servicos_da_rota(r)
            for s in servicos_rota:
                todos_servicos_por_id[s["id"]] = s
            info_rotas.append({
                "id": r["id"], "nome": r["name"],
                "centroide": _centroide_rota(r, gmaps_key), "qtd": len(servicos_rota),
                "caixas": sum(extrair_volume_caixas(s) for s in servicos_rota),
                "service_ids": [s["id"] for s in servicos_rota],
                "agent_id": r.get("agent_id"),
                "vehicle_id": r.get("vehicle_id"),
            })

        ids_rotas_existentes_desde_inicio = {info["id"] for info in info_rotas}

        # Semeia a contagem do dia com os motoristas JÁ atribuídos nas
        # rotas de hoje (fonte: estado real da API, não um contador
        # zerado a cada execução) -- pra que a distribuição equitativa
        # de rotas NOVAS (via criar_rota mais abaixo) considere também o
        # que o job das 13h (criar_rotas_diarias.py) e incrementos
        # anteriores já alocaram no mesmo dia.
        contagem_alocacoes_dia: dict[int, int] = {}
        for info in info_rotas:
            if info["agent_id"] is not None:
                contagem_alocacoes_dia[info["agent_id"]] = contagem_alocacoes_dia.get(info["agent_id"], 0) + 1

        alocados = 0
        rotas_novas = 0
        rotas_afetadas: set[int] = set()
        orfaos = []
        proximo_indice = _proximo_indice_disponivel(rotas_hoje)

        for pedido in novos:
            coords_pedido = obter_coordenadas(pedido, gmaps_key)
            cx_pedido = extrair_volume_caixas(pedido)
            alocado_em_existente = False
            pular_pedido = False  # erro desconhecido -- não vira órfão, só pula (comportamento original)

            # Trava de Viagem (doc de alocação, item 3.5): se ESSE pedido
            # sozinho já é "Viagem" (fora da Grande SP), só pode entrar
            # numa rota existente cujo motorista atribuído aceite viagem
            # -- nunca transforma uma rota urbana em viagem sem a
            # permissão do motorista que já está nela. Rota sem motorista
            # atribuído ainda (agent_id=None) não bloqueia -- nenhuma
            # permissão está sendo violada nesse caso.
            pedido_eh_viagem = classificar_rota_viagem([pedido], gmaps_key)

            # Trava de Zona (pedido do Hugo, 10/08): mesma lógica, mas
            # pra preferência de área dentro da Grande SP -- só se
            # aplica quando o pedido NÃO é viagem (zona não importa pra
            # motorista de viagem). Rota com motorista cuja zona não
            # bate com a do pedido é excluída das candidatas, igual à
            # trava de viagem acima.
            pedido_zona = None if pedido_eh_viagem else classificar_zona(pedido, gmaps_key)

            # Tenta a melhor rota candidata; se o VUUPT recusar por causa
            # do STATUS da rota (achado em produção, 06/08: rota "Cancelada"
            # ainda aparecia como candidata -- data óbvia, mas cancelada
            # por fora, ex: manualmente no VUUPT -- e o POST falhava com
            # 400 "A rota não pode ser alterada no status atual"), remove
            # SÓ essa rota específica da lista de candidatas e tenta de
            # novo com a próxima melhor, pro MESMO pedido -- não perde o
            # pedido por causa de 1 rota problemática, igual já fazemos
            # em criar_rotas_diarias.py pro caso de conflito de serviço.
            # Erro de QUALQUER OUTRO tipo (desconhecido) NÃO vira órfão --
            # só pula esse pedido nessa rodada (comportamento original,
            # mais seguro pra erro que não entendemos: fica not_assigned,
            # tenta de novo automaticamente na próxima hora).
            while True:
                # Duas listas separadas (achado em produção, 02/08: uma rota
                # criada pra um pedido SEM coordenada ficava com centroide=None
                # e nunca mais podia ser escolhida por NINGUÉM, nem pelo
                # caminho de reserva por espaço). O caminho de reserva (sem
                # coordenada do pedido) só precisa de ESPAÇO, não de
                # centroide -- então usa a lista mais ampla.
                candidatas_com_espaco = [
                    r for r in info_rotas
                    if r["qtd"] < TAMANHO_MAXIMO_ROTA and r["caixas"] + cx_pedido <= VOLUME_MAXIMO_ROTA
                    and (
                        not pedido_eh_viagem
                        or r["agent_id"] is None
                        or motoristas_por_id.get(r["agent_id"], None) is not None
                        and motoristas_por_id[r["agent_id"]].aceita_viagens
                    )
                    and (
                        pedido_zona is None
                        or r["agent_id"] is None
                        or motoristas_por_id.get(r["agent_id"], None) is not None
                        and pedido_zona in motoristas_por_id[r["agent_id"]].zonas_preferidas
                    )
                ]
                candidatas_com_centroide = [r for r in candidatas_com_espaco if r["centroide"]]

                rota_escolhida = None
                if coords_pedido and candidatas_com_centroide:
                    # Trava de distância (achado em produção, 09/08: PS-36198,
                    # em Niterói-RJ, foi parar numa rota de São Paulo por ser
                    # "a mais próxima com espaço" -- sem limite absoluto, o
                    # centroide mais próximo pode ainda estar a centenas de
                    # km) -- só considera rota cujo centroide esteja a até
                    # DISTANCIA_MAXIMA_ROTA_KM do pedido; nenhuma dentro do
                    # raio -- vira órfão, igual pedido sem rota nenhuma perto.
                    candidatas_no_raio = [
                        r for r in candidatas_com_centroide
                        if _distancia_km(*coords_pedido, *r["centroide"]) <= DISTANCIA_MAXIMA_ROTA_KM
                    ]
                    if candidatas_no_raio:
                        rota_escolhida = min(
                            candidatas_no_raio, key=lambda r: _distancia_km(*coords_pedido, *r["centroide"])
                        )
                elif candidatas_com_espaco:
                    # sem coordenada do pedido (ou nenhuma rota com centroide
                    # disponível) -- pega a rota com mais espaço livre, último recurso
                    rota_escolhida = max(candidatas_com_espaco, key=lambda r: TAMANHO_MAXIMO_ROTA - r["qtd"])

                if not rota_escolhida:
                    break  # nenhuma candidata restante -- vira órfão

                logger.info(f"  {pedido.get('code')} -> rota existente '{rota_escolhida['nome']}' "
                           f"({rota_escolhida['qtd']}/{TAMANHO_MAXIMO_ROTA})")

                if modo_teste:
                    alocado_em_existente = True
                    break

                try:
                    # position="optimize" EXIGE veículo atribuído na rota pra
                    # calcular a melhor posição (achado em produção, 06/08 --
                    # "Selecione um veículo para otimizar a rota", 400 em TODA
                    # tentativa) -- rotas aqui nunca têm veículo (atribuição
                    # manual, decisão do Hugo). "end" não exige isso; a ordem
                    # final não importa de qualquer forma, porque o bloco de
                    # resequenciamento logo abaixo reordena a rota inteira por
                    # distância da base depois de qualquer pedido novo.
                    adicionar_atividades(token, rota_escolhida["id"], [pedido["id"]], position="end")
                    alocado_em_existente = True
                    break
                except Exception as e:
                    if PADRAO_ROTA_INVALIDA.search(str(e)):
                        logger.warning(f"  Rota '{rota_escolhida['nome']}' (id={rota_escolhida['id']}) não "
                                      f"pode mais receber pedido ({e}) -- removendo da lista de candidatas "
                                      f"e tentando outra rota pro mesmo pedido.")
                        info_rotas.remove(rota_escolhida)
                        continue
                    logger.error(f"  Falha ao adicionar {pedido.get('code')} na rota {rota_escolhida['id']}: {e}")
                    pular_pedido = True
                    break

            if alocado_em_existente:
                rota_escolhida["qtd"] += 1
                rota_escolhida["caixas"] += cx_pedido
                rota_escolhida["service_ids"].append(pedido["id"])
                todos_servicos_por_id[pedido["id"]] = pedido
                rotas_afetadas.add(rota_escolhida["id"])
                alocados += 1
            elif pular_pedido:
                continue  # erro desconhecido -- não tenta órfão, deixa not_assigned pra próxima rodada
            else:
                # Nenhuma rota existente com espaço -- vira órfão, pra
                # ser agrupado com outros órfãos antes de criar rota
                # nova (pedido do Hugo, 02/08: agrupar entre si em vez
                # de 1 rota por pedido).
                orfaos.append(pedido)

        # Agrupa os órfãos entre si (mesma lógica geográfica do job das
        # 13h: região -> consolida até o mínimo -> divide em sublotes
        # balanceados de até o máximo) antes de criar rotas novas --
        # em vez de 1 rota nova por pedido individual.
        if orfaos:
            logger.info(f"{len(orfaos)} pedido(s) sem correspondência em rota existente -- agrupando entre si.")
            grupos_iniciais_orfaos = agrupar_por_regiao(orfaos, api_key=gmaps_key)
            grupos_orfaos = consolidar_regioes_pequenas(grupos_iniciais_orfaos, minimo=TAMANHO_MINIMO_ROTA, api_key=gmaps_key)

            for servicos_regiao in grupos_orfaos.values():
                # Mesmo critério de criar_rotas_diarias.py: região de
                # Viagem não tem limite de distância entre pedidos.
                distancia_maxima_da_regiao = (
                    DISTANCIA_MAXIMA_VIAGEM_KM if classificar_rota_viagem(servicos_regiao, gmaps_key)
                    else DISTANCIA_MAXIMA_ROTA_KM
                )
                sublotes = dividir_em_sublotes(servicos_regiao, tamanho_minimo=TAMANHO_MINIMO_ROTA,
                                              tamanho_maximo=TAMANHO_MAXIMO_ROTA,
                                              volume_maximo=VOLUME_MAXIMO_ROTA,
                                              distancia_maxima_km=distancia_maxima_da_regiao, api_key=gmaps_key)
                for sublote in sublotes:
                    nome_rota = f"{prefixo_hoje} - #{proximo_indice}"
                    proximo_indice += 1

                    if coords_base:
                        sublote = ordenar_por_distancia_base(sublote, coords_base[0], coords_base[1], gmaps_key)

                    codigos = [s.get("code") for s in sublote]
                    service_ids = [s["id"] for s in sublote]

                    eh_viagem = classificar_rota_viagem(sublote, gmaps_key)
                    tipo_rota_str = "VIAGEM" if eh_viagem else f"Grande SP/{classificar_rota_zona(sublote, gmaps_key) or '?'}"
                    motorista = selecionar_motorista_equitativo(
                        sublote, data_alvo, catalogo_motoristas.motoristas, contagem_alocacoes_dia, gmaps_key,
                    )
                    agent_id = motorista.agent_id if motorista else None
                    vehicle_id = motorista.vehicle_id if motorista else None
                    motorista_str = motorista.nome if motorista else "SEM MOTORISTA [ALERTA_ALOCACAO]"

                    logger.info(f"  Rota NOVA '{nome_rota}' [{tipo_rota_str}]: {len(sublote)} pedido(s) agrupados "
                               f"(mais longe -> mais perto da base) -- motorista: {motorista_str}: {codigos}")

                    if not modo_teste:
                        try:
                            rota = criar_rota(
                                token, nome=nome_rota, start_at=f"{data_alvo_str}T13:00:00Z",
                                start_location_base_id=BASE_LOCATION_ID, service_ids=service_ids,
                                end_location_base_id=BASE_LOCATION_ID,
                                agent_id=agent_id, vehicle_id=vehicle_id,
                            )
                            info_rotas.append({
                                "id": rota["id"], "nome": nome_rota,
                                "centroide": None, "qtd": len(sublote),
                                "caixas": sum(extrair_volume_caixas(s) for s in sublote),
                                "service_ids": service_ids,
                                "agent_id": agent_id, "vehicle_id": vehicle_id,
                            })
                            rotas_afetadas.add(rota["id"])
                        except Exception as e:
                            logger.error(f"  Falha ao criar rota nova pro grupo '{nome_rota}': {e}")
                            continue
                    if motorista:
                        contagem_alocacoes_dia[motorista.agent_id] = contagem_alocacoes_dia.get(motorista.agent_id, 0) + 1
                    else:
                        rotas_sem_motorista += 1
                    rotas_novas += 1
                    alocados += len(sublote)

        # Resequenciamento (pedido do Hugo, 03/08: rotas sempre da mais
        # LONGE pra mais PERTO da base) -- só nas rotas que JÁ
        # EXISTIAM antes desse run e ganharam pedido novo (rotas
        # criadas agora mesmo já nasceram na ordem certa, na etapa
        # acima -- reaplicar aqui seria desnecessário).
        if not modo_teste and coords_base:
            for rota_info in info_rotas:
                if rota_info["id"] not in rotas_afetadas or rota_info["id"] not in ids_rotas_existentes_desde_inicio:
                    continue
                try:
                    servicos_da_rota = [
                        todos_servicos_por_id[sid] for sid in rota_info["service_ids"]
                        if sid in todos_servicos_por_id
                    ]
                    if len(servicos_da_rota) != len(rota_info["service_ids"]):
                        logger.warning(f"  Rota '{rota_info['nome']}': faltam dados de algum serviço pra "
                                      f"reordenar -- mantendo ordem atual.")
                        continue
                    ordenados = ordenar_por_distancia_base(servicos_da_rota, coords_base[0], coords_base[1], gmaps_key)
                    ordem_ids = [s["id"] for s in ordenados]
                    if ordem_ids != rota_info["service_ids"]:
                        atualizar_rota(token, rota_info["id"], ordem_ids)
                        logger.info(f"  Rota '{rota_info['nome']}' reordenada (mais longe -> mais perto da base).")
                except Exception as e:
                    logger.warning(f"  Falha ao reordenar a rota '{rota_info['nome']}' "
                                  f"(rota continua com a ordem atual): {e}")

        prefixo_teste = "[Teste] " if modo_teste else ""
        resumo_etapas["Incremento de rotas"] = {
            "status": "ok",
            "detalhe": f"{prefixo_teste}{alocados} pedido(s) alocado(s) ({rotas_novas} rota(s) nova(s) criada(s)).",
        }

        if rotas_novas:
            matriz_alocacao = ", ".join(
                f"{next((m.nome for m in catalogo_motoristas.motoristas if m.agent_id == agent_id), agent_id)}: {qtd}"
                for agent_id, qtd in sorted(contagem_alocacoes_dia.items(), key=lambda item: -item[1])
            ) or "nenhuma"
            resumo_etapas["Alocação de motoristas (rotas novas)"] = {
                "status": "ok" if rotas_sem_motorista == 0 else "erro",
                "detalhe": f"{prefixo_teste}Distribuição do dia: {matriz_alocacao}."
                          + (f" [ALERTA_ALOCACAO] {rotas_sem_motorista} rota(s) nova(s) sem motorista disponível."
                             if rotas_sem_motorista else ""),
            }

    except Exception as e:
        logger.exception(f"Erro no incremento de rotas: {e}")
        resumo_etapas["Incremento de rotas"] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"Incremento de rotas finalizado em {duracao:.1f}s.")

    try:
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aloca pedidos novos nas rotas do dia já criadas")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Mostra o que seria alocado, sem chamar a API de verdade")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
