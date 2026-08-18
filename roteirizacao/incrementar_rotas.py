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

Se nenhuma rota existente tiver espaço/compatibilidade pro pedido,
ele fica not_assigned e passa pro dia seguinte (pedido do Hugo,
18/08 -- o incremento NUNCA cria rota nova; antes criava uma rota
nova por região pros órfãos, mas isso vinha gerando rotas pequenas
demais e fragmentadas, várias vezes até sem motorista disponível pro
horário). `criar_rotas_diarias.py` recolhe esses órfãos no rascunho
do dia seguinte, junto com o resto do lote.

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
from collections import Counter
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

from roteirizacao_dados import obter_coordenadas, elegivel_para_data, extrair_volume_caixas, _distancia_km, ordenar_por_distancia_base, macro_regiao_do_servico
from rotas_client import listar_rotas, adicionar_atividades, atualizar_rota
from criar_rotas_diarias import (
    ENDERECO_BASE, PREFIXO_NOME_ROTA, TAMANHO_MAXIMO_ROTA, VOLUME_MAXIMO_ROTA,
    DISTANCIA_MAXIMA_ROTA_KM, TZ_BRASILIA, _data_alvo_rotas,
)
from notificar_agendamento_pendente import identificar_pendentes
from regras.clientes_agendamento import carregar_clientes_agendamento, tem_agendamento
from agendamento_confirmacao import buscar_confirmacao
from regioes_dia_fixo import aplicar_regioes_dia_fixo
from notificar_agendamento_dia_fixo import notificar_agendamentos_dia_fixo
from notificar_area_nao_atendida import identificar_area_nao_atendida, notificar_remetentes as notificar_area_nao_atendida
from regras.preferencias_motoristas import CatalogoMotoristas
from alocacao_motoristas import classificar_rota_viagem
from zonas_sp import classificar_zona
from regras.tipo_veiculo import classificar_tipo_veiculo


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
# que o motorista ainda não começou a rodar. "scheduled" (14/08, também
# confirmado contra a API: rota com start_at futuro e sem agent_id --
# mesmo perfil de "not_assigned") entra no mesmo grupo. "started" continua
# bloqueado (rota em execução -- é exatamente o caso que motivou a trava
# original, 06/08: motorista reclamou de pedido aparecendo do nada numa
# rota já na rua). Qualquer status não reconhecido (novo/desconhecido)
# também bloqueia, por segurança -- só libera pros valores explicitamente
# confirmados como seguros.
STATUS_ROTA_HOJE_LIBERADOS = {"not_started", "assigned", "accepted", "not_assigned", "scheduled"}


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


def _cabe_na_rota(rota: dict, cx_pedido: int, endereco_pedido: str | None) -> bool:
    """
    True se `cx_pedido` (do endereço `endereco_pedido`) ainda cabe na
    rota EXISTENTE `rota` (dict de info_rotas, ver main()).

    Rota já classificada como veículo grande (`rota["tipo_veiculo"]`,
    ver regras/tipo_veiculo.py) respeita o teto de caixas E de
    endereços diferentes do PRÓPRIO tipo -- em vez do teto genérico de
    última milha (`TAMANHO_MAXIMO_ROTA`/`VOLUME_MAXIMO_ROTA`), que não
    faz sentido pra uma rota dessas (pode ter dezenas de pedidos pro
    MESMO endereço, e cabe bem mais que 100 caixas). Rota comum segue a
    trava genérica de sempre, sem mudança nenhuma.
    """
    tipo = rota["tipo_veiculo"]
    if tipo is not None:
        caixas_cabe = rota["caixas"] + cx_pedido <= tipo.volume_maximo_cx
        enderecos_cabe = len(rota["enderecos"] | {endereco_pedido}) <= tipo.max_enderecos_distintos
        return caixas_cabe and enderecos_cabe
    return rota["qtd"] < TAMANHO_MAXIMO_ROTA and rota["caixas"] + cx_pedido <= VOLUME_MAXIMO_ROTA


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
            agendados_dia_fixo = aplicar_regioes_dia_fixo(servicos, vuupt)
            if agendados_dia_fixo:
                logger.info(f"{len(agendados_dia_fixo)} pedido(s) agendado(s) por região de dia fixo.")
                # Avisa o remetente da data agendada (pedido do Hugo, 12/08).
                resultado_aviso = notificar_agendamentos_dia_fixo(
                    agendados_dia_fixo, config.get("email", {}), modo_teste=modo_teste)
                logger.info(f"Notificação de agendamento por dia fixo: {resultado_aviso}")
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

        # TRAVA DE ROTA CONFIRMADA (14/08, pedido do Hugo): desde que
        # criar_rotas_diarias.py passou a gerar rascunho em vez de
        # criar direto (13/08), o envio automático do rascunho pendente
        # (enviar_rascunhos_pendentes.py, rede de segurança da
        # sequência da noite) foi CANCELADO -- se ninguém confirmar o
        # rascunho em /planejamento até o incremento rodar, não existe
        # nenhuma rota "{prefixo_hoje}*" na VUUPT ainda. Antes, esse
        # caso caía direto na criação de rota nova do zero, duplicando
        # o que já estava desenhado no rascunho. Agora falha explícito
        # (status erro + e-mail de alerta) em vez de mascarar o
        # esquecimento com uma duplicidade silenciosa.
        if not rotas_hoje:
            raise RuntimeError(
                f"Nenhuma rota de hoje ('{prefixo_hoje}*') encontrada na VUUPT -- rascunho de "
                f"{data_alvo_br} não foi confirmado em /planejamento. {len(novos)} pedido(s) novo(s) "
                f"ficaram sem alocar; confirme o rascunho e rode o incremento de novo."
            )

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
            # Macro-região da rota (trava de macro, pedido do Hugo,
            # 12/08): a mais FREQUENTE entre as entregas (moda) -- rotas
            # criadas depois da trava são de macro única por construção;
            # a moda cobre rota antiga/manual eventualmente mista. Rota
            # sem serviço classificável: None (não restringe).
            macros_rota = [macro_regiao_do_servico(s, gmaps_key) for s in servicos_rota]
            macro_rota = Counter(macros_rota).most_common(1)[0][0] if macros_rota else None
            enderecos_rota = {s.get("address") for s in servicos_rota}
            caixas_rota = sum(extrair_volume_caixas(s) for s in servicos_rota)
            info_rotas.append({
                "id": r["id"], "nome": r["name"],
                "centroide": _centroide_rota(r, gmaps_key), "qtd": len(servicos_rota),
                "caixas": caixas_rota,
                # Endereços distintos + tipo de veículo grande já
                # classificado pra essa rota (regras/tipo_veiculo.py,
                # pedido do Hugo, 15/08) -- None pra rota comum de
                # última milha, mantém as travas de sempre (ver
                # _cabe_na_rota, abaixo). Rota que NASCEU classificada
                # (criar_rotas_diarias.py) continua respeitando o teto
                # do PRÓPRIO tipo ao receber pedido novo por hora, em
                # vez do teto genérico de 100 caixas/16 paradas.
                "enderecos": enderecos_rota,
                "tipo_veiculo": classificar_tipo_veiculo(caixas_rota, len(enderecos_rota)),
                "service_ids": [s["id"] for s in servicos_rota],
                "agent_id": r.get("agent_id"),
                "vehicle_id": r.get("vehicle_id"),
                "macro": macro_rota,
            })

        ids_rotas_existentes_desde_inicio = {info["id"] for info in info_rotas}

        alocados = 0
        rotas_afetadas: set[int] = set()
        orfaos = []

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

            # Trava de MACRO-REGIÃO (pedido do Hugo, 12/08: "pedidos de
            # Sorocaba não se misturariam com pedidos de Barueri"):
            # pedido só entra em rota existente da MESMA macro-região
            # (Grande SP x cada região externa). A trava de distância de
            # 20km no centroide (abaixo) já barra a maioria dos casos,
            # mas não todos -- ex: rota só de Suzano (Vale do Paraíba)
            # tem centroide a ~14km de Itaquera, dentro do raio.
            pedido_macro = macro_regiao_do_servico(pedido, gmaps_key)

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
                    if _cabe_na_rota(r, cx_pedido, pedido.get("address"))
                    and (r["macro"] is None or r["macro"] == pedido_macro)
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
                elif not pedido_eh_viagem and candidatas_com_espaco:
                    # sem coordenada do pedido (ou nenhuma rota com centroide
                    # disponível) -- pega a rota com mais espaço livre, último
                    # recurso. Pedido de VIAGEM nunca usa esse caminho (trava
                    # de macro-região, pedido do Hugo, 12/08): sem coordenada
                    # não dá pra verificar proximidade, mas a CIDADE já diz
                    # que ele é fora da Grande SP -- entrar na rota "com mais
                    # espaço" misturaria Sorocaba com rota urbana. Vira órfão
                    # e se agrupa com a própria macro-região logo abaixo.
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
                rota_escolhida["enderecos"].add(pedido.get("address"))
                rota_escolhida["service_ids"].append(pedido["id"])
                todos_servicos_por_id[pedido["id"]] = pedido
                rotas_afetadas.add(rota_escolhida["id"])
                alocados += 1
            elif pular_pedido:
                continue  # erro desconhecido -- não tenta órfão, deixa not_assigned pra próxima rodada
            else:
                # Nenhuma rota existente com espaço/compatibilidade --
                # vira órfão. O incremento NUNCA cria rota nova (pedido
                # do Hugo, 18/08 -- antes agrupava os órfãos e criava
                # uma rota nova por região, mas isso vinha saindo
                # fragmentado demais: várias rotas pequenas, algumas
                # com só 1 pedido e sem motorista disponível pro
                # horário). Fica not_assigned; `criar_rotas_diarias.py`
                # recolhe no rascunho do dia seguinte.
                orfaos.append(pedido)

        if orfaos:
            logger.info(f"{len(orfaos)} pedido(s) sem correspondência em rota existente -- "
                       f"ficam not_assigned pro rascunho do dia seguinte: "
                       f"{[p.get('code') for p in orfaos]}")

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
            "detalhe": f"{prefixo_teste}{alocados} pedido(s) alocado(s) em rota existente"
                      + (f", {len(orfaos)} pedido(s) sem rota com espaço/compatibilidade hoje "
                         f"(ficam pro rascunho do dia seguinte)." if orfaos else "."),
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
