# -*- coding: utf-8 -*-
"""
criar_rotas_diarias.py

Job das 13h (pedido do Hugo, 01/08): pega os pedidos not_assigned do
momento, agrupa geograficamente (mesma lógica de roteirizacao_dados.py
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

Rotas ficam com start_at = amanhã (o dia que estão sendo planejadas),
e o NOME segue exatamente o padrão nativo do VUUPT (mesma convenção
usada pela própria tela, confirmada com dado real): "Planejamento -
DD/MM/AAAA - #N", numeração GLOBAL pro dia (não por região) -- pra
incrementar_rotas.py conseguir encontrá-las depois.

COMO USAR:
    py -3.11 criar_rotas_diarias.py                # execução normal
    py -3.11 criar_rotas_diarias.py --modo-teste    # só mostra o que criaria
"""
import argparse
import logging
import re
import sys
import time
from datetime import date, timedelta
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

from roteirizacao_dados import agrupar_por_regiao, consolidar_regioes_pequenas, dividir_em_sublotes, elegivel_para_data, ordenar_por_distancia_base
from rotas_client import criar_rota
from fingerprint_rotas import marcar_alocado
from notificar_agendamento_pendente import identificar_pendentes, notificar_remetentes
from regras.clientes_agendamento import carregar_clientes_agendamento, tem_agendamento
from agendamento_confirmacao import buscar_confirmacao
from regioes_dia_fixo import aplicar_regioes_dia_fixo
from notificar_area_nao_atendida import identificar_area_nao_atendida, notificar_remetentes as notificar_area_nao_atendida

ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"
BASE_LOCATION_ID = 6950  # confirmado em produção (operational_base_id da base, visto em dados reais do VUUPT)
TAMANHO_MINIMO_ROTA = 10
TAMANHO_MAXIMO_ROTA = 15
PREFIXO_NOME_ROTA = "Planejamento"


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


PADRAO_CONFLITO_ROTA = re.compile(
    r"j[aá] faz parte de uma rota.*?(PS-\d+)", re.IGNORECASE | re.DOTALL
)


def _criar_rota_removendo_conflitos(token: str, nome_rota: str, start_at: str,
                                     sublote: list[dict], max_tentativas: int = 8):
    """
    Cria a rota com o sublote dado -- se o VUUPT recusar por algum
    serviço já estar em OUTRA rota (achado em produção, 06/08: a lista
    'not_assigned' pode ficar levemente desatualizada entre a busca e
    a criação de fato, e a API rejeita o POST INTEIRO por causa de 1
    serviço só, perdendo os outros 10-15 pedidos legítimos do lote
    junto), remove só o(s) serviço(s) apontado(s) no erro e tenta de
    novo -- não perde o lote inteiro por causa de 1 entrada obsoleta.

    Retorna (rota_criada_ou_None, sublote_final_usado, codigos_removidos).
    Se sobrar 0 serviços ou passar de max_tentativas, retorna
    (None, [], codigos_removidos) -- quem chama decide como logar.
    """
    sublote_atual = list(sublote)
    codigos_removidos = []

    for _ in range(max_tentativas):
        if not sublote_atual:
            return None, [], codigos_removidos

        service_ids = [s["id"] for s in sublote_atual]
        try:
            rota = criar_rota(
                token, nome=nome_rota, start_at=start_at,
                start_location_base_id=BASE_LOCATION_ID, service_ids=service_ids,
                end_location_base_id=BASE_LOCATION_ID,
            )
            return rota, sublote_atual, codigos_removidos
        except Exception as e:
            match = PADRAO_CONFLITO_ROTA.search(str(e))
            if not match:
                raise  # erro de outro tipo -- não sabemos remediar, propaga como antes

            codigo_conflitante = match.group(1)
            antes = len(sublote_atual)
            sublote_atual = [s for s in sublote_atual if s.get("code", "").lstrip("#") != codigo_conflitante]
            if len(sublote_atual) == antes:
                # o código apontado no erro não bate com nenhum do sublote atual
                # (já deve ter sido removido numa tentativa anterior, ou algo
                # mudou) -- evita loop infinito reenviando o mesmo pedido
                raise
            codigos_removidos.append(codigo_conflitante)
            logger.warning(f"  '{nome_rota}': {codigo_conflitante} já está em outra rota (dado desatualizado) "
                           f"-- removendo do lote e tentando de novo com os {len(sublote_atual)} restante(s).")

    raise Exception(f"Excedeu {max_tentativas} tentativas removendo conflitos -- "
                    f"removidos até agora: {codigos_removidos}")


def main(modo_teste: bool = False):
    inicio = time.time()
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Criação de rotas diárias iniciada.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    resumo_etapas = {}

    try:
        vuupt = VuuptClient(token)

        amanha = date.today() + timedelta(days=1)
        amanha_str = amanha.strftime("%Y-%m-%d")
        amanha_br = amanha.strftime("%d/%m/%Y")  # formato usado no NOME da rota (convenção nativa do VUUPT)

        filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
        servicos_brutos = vuupt.listar_servicos(filtro, per_page=100)
        logger.info(f"{len(servicos_brutos)} serviço(s) 'not_assigned' encontrado(s).")

        # Regiões com dia fixo de entrega (pedido do Hugo, 02/08) --
        # roda ANTES de tudo: define scheduled_start pra quem ainda
        # não tem e mora numa cidade de dia fixo, refletindo já no
        # dict em memória (o resto do fluxo, incluindo o filtro de
        # elegibilidade abaixo, já lida com isso automaticamente).
        try:
            qtd_agendados_dia_fixo = aplicar_regioes_dia_fixo(servicos_brutos, vuupt)
            if qtd_agendados_dia_fixo:
                logger.info(f"{qtd_agendados_dia_fixo} pedido(s) agendado(s) por região de dia fixo.")
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
            if elegivel_para_data(s, amanha)
            and s["id"] not in ids_pendentes_notificacao
            and s["id"] not in ids_area_nao_atendida
        ]
        adiados = len(servicos_brutos) - len(servicos)
        if adiados:
            logger.info(f"{adiados} pedido(s) com agendamento futuro, pendente de resposta, ou área não atendida -- adiados.")

        if not servicos:
            resumo_etapas["Criação de rotas"] = {"status": "ok", "detalhe": "Nenhum pedido not_assigned elegível."}
            return

        grupos_iniciais = agrupar_por_regiao(servicos, api_key=gmaps_key)
        logger.info(f"{len(grupos_iniciais)} região(ões) geográfica(s) inicial(is).")
        grupos_validos = consolidar_regioes_pequenas(grupos_iniciais, minimo=TAMANHO_MINIMO_ROTA, api_key=gmaps_key)
        logger.info(f"Após consolidar regiões pequenas: {len(grupos_validos)} região(ões) final(is).")

        if not grupos_validos:
            resumo_etapas["Criação de rotas"] = {"status": "ok", "detalhe": "Nenhum pedido elegível."}
            return

        start_at = f"{amanha_str}T13:00:00Z"

        # Base pra ordenar cada rota da mais LONGE pra mais PERTO
        # (pedido do Hugo, 03/08 -- padrão de sequenciamento). Se
        # falhar, a criação de rotas segue com a ordem original de
        # proximidade (já razoável).
        coords_base = None
        if not modo_teste:
            try:
                coords_base = geocodificar(ENDERECO_BASE, gmaps_key)
            except Exception as e:
                logger.warning(f"Não consegui geocodificar a base pra ordenar as rotas: {e}")

        rotas_criadas = 0
        pedidos_alocados = 0
        indice_global = 1
        for regiao, servicos_regiao in grupos_validos.items():
            sublotes = dividir_em_sublotes(servicos_regiao, tamanho_minimo=TAMANHO_MINIMO_ROTA,
                                          tamanho_maximo=TAMANHO_MAXIMO_ROTA, api_key=gmaps_key)
            for sublote in sublotes:
                nome_rota = f"{PREFIXO_NOME_ROTA} - {amanha_br} - #{indice_global}"
                indice_global += 1

                # Ordena da mais LONGE pra mais PERTO da base ANTES de
                # criar a rota, pra já nascer na ordem certa (pedido do
                # Hugo, 03/08) -- sem precisar de um passo de
                # sequenciamento separado depois.
                if coords_base:
                    sublote = ordenar_por_distancia_base(sublote, coords_base[0], coords_base[1], gmaps_key)

                codigos = [s.get("code") for s in sublote]
                service_ids = [s["id"] for s in sublote]

                if modo_teste:
                    logger.info(f"[TESTE] Criaria rota '{nome_rota}' com {len(sublote)} pedido(s) "
                               f"(mais longe -> mais perto da base): {codigos}")
                    rotas_criadas += 1
                    pedidos_alocados += len(sublote)
                    continue

                try:
                    rota, sublote_criado, codigos_removidos = _criar_rota_removendo_conflitos(
                        token, nome_rota, start_at, sublote,
                    )
                    if rota is None:
                        logger.error(f"'{nome_rota}': sobrou 0 pedido(s) depois de remover conflitos "
                                    f"({codigos_removidos}) -- nenhuma rota criada pra esse lote.")
                        continue

                    codigos_finais = [s.get("code") for s in sublote_criado]
                    aviso_removidos = f" (removidos por conflito: {codigos_removidos})" if codigos_removidos else ""
                    logger.info(f"Rota criada: '{nome_rota}' (id={rota['id']}) com {len(sublote_criado)} pedido(s) "
                               f"(mais longe -> mais perto da base){aviso_removidos}: {codigos_finais}")
                    for s in sublote_criado:
                        marcar_alocado(s["id"], rota["id"])
                    rotas_criadas += 1
                    pedidos_alocados += len(sublote_criado)
                except Exception as e:
                    logger.error(f"Falha ao criar rota '{nome_rota}': {e}")

        prefixo_teste = "[Teste] " if modo_teste else ""
        resumo_etapas["Criação de rotas"] = {
            "status": "ok",
            "detalhe": f"{prefixo_teste}{rotas_criadas} rota(s) criada(s), {pedidos_alocados} pedido(s) alocado(s), "
                      f"sem veículo atribuído (atribuição manual).",
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
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
