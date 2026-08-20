# -*- coding: utf-8 -*-
"""
lancar_coleta.py

Rotina diária (08:00, dias úteis -- ver infra/stokki-coleta-emporio-
quatro-estrelas.timer): lança na VUUPT o pedido de coleta do dia no
Empório Quatro Estrelas (pedido do Hugo, 20/08), alternando o motorista
responsável entre Vinícius Oliveira Correia (agent_id 47084) e Erick
Gomes Oliveira (agent_id 47424).

Só a VUUPT é usada aqui (não a Stokki -- confirmado com o Hugo, 20/08).

Payload confirmado batendo EXATAMENTE contra pedidos de coleta reais já
lançados manualmente na conta (mesmo customer_id, mesmo code/title
literal, mesma base/horário de rota -- inclusive um lançado manualmente
minutos antes desta rotina ser escrita, service 52363342/route 5111257,
20/08, motorista Vinícius). Note que o customer_id da COLETA (11249509,
"COMERCIO DE CEREAIS QUATRO ESTRELAS LTDA", Rua Caraíbas 351/355,
Pompeia) é DIFERENTE do sender_id usado nas entregas DE SAÍDA do mesmo
cliente (21785428, endereço da Lapa) -- são pontos físicos distintos,
não confundir. `code`/`title` também seguem o padrão real: sempre o
literal "COLETA QUATRO ESTRELAS" (sem sufixo de data -- confirmado que
a API do VUUPT não exige `code` único), sem `sender_id`.

Regra de rodízio (pedido explícito do Hugo, 20/08 -- FIXA por dia da
semana, não depende da placa/módulo roteirizacao/rodizio_sp.py):
    - Segunda-feira: Vinícius está de rodízio -- só Erick pode coletar.
    - Sexta-feira:   Erick está de rodízio    -- só Vinícius pode coletar.
    - Terça a quinta: os dois estão elegíveis.

Divisão equalitária: em vez de um padrão fixo por dia da semana (que
não fecha 50/50 numa semana de 5 dias -- segunda já é sempre Erick e
sexta já é sempre Vinícius), guarda-se uma CONTAGEM cumulativa por
motorista (dados/estado_alternancia.json) e, nos dias em que os dois
estão elegíveis, escolhe sempre quem tem MENOS coletas acumuladas até
agora. Isso equilibra o total no longo prazo mesmo se a rotina falhar
ou pular algum dia (feriado, instabilidade) -- um padrão fixo por dia
da semana não se autocorrige nesses casos.

Idempotente por DATA: antes de criar, busca se já existe algum serviço
de coleta pra esse cliente criado HOJE (ver _buscar_coleta_de_hoje) --
se sim (feito manualmente por alguém ou por uma execução anterior desta
rotina), não duplica; só cria um novo se não houver nenhum de hoje.

COMO USAR:
    py -3.11 lancar_coleta.py                # execução normal
    py -3.11 lancar_coleta.py --modo-teste    # só mostra o que faria, não grava nada nem altera o estado
"""
import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_PROJETO / "roteirizacao"))
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
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "lancar_coleta.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("coleta_emporio_quatro_estrelas")

import json

import yaml

from vuupt_client import VuuptClient
from rotas_client import criar_rota
from notificar_execucao_agente import notificar_execucao
from http_retry import chamar_com_retry

CUSTOMER_ID_COLETA = 11249509  # "COMERCIO DE CEREAIS QUATRO ESTRELAS LTDA", Rua Caraíbas 351/355, Pompeia --
# ponto de COLETA de verdade (confirmado 20/08 contra pedidos reais já lançados manualmente na
# conta). NÃO confundir com o sender_id 21785428 (Rua Herbart, Lapa), usado nas entregas DE SAÍDA
# desse mesmo cliente -- são pontos físicos diferentes.
CODIGO_TITULO_COLETA = "COLETA QUATRO ESTRELAS"  # literal, sem sufixo de data -- mesma convenção usada
# em todo lançamento manual histórico da conta; a API do VUUPT não exige `code` único.
BASE_LOCATION_ID = 6950  # mesma base operacional usada em roteirizacao/criar_rotas_diarias.py e nas rotas reais de coleta

AGENT_ID_VINICIUS = 47084
AGENT_ID_ERICK = 47424
NOMES_MOTORISTAS = {
    AGENT_ID_VINICIUS: "Vinícius Oliveira Correia",
    AGENT_ID_ERICK: "Erick Gomes Oliveira",
}

ESTADO_PATH = _RAIZ_LOCAL / "dados" / "estado_alternancia.json"


def _buscar_coleta_de_hoje(vuupt: VuuptClient, hoje: date) -> dict | None:
    """Busca um serviço de coleta já criado HOJE pra esse cliente.

    NÃO usa VuuptClient.buscar_servico_por_code: essa função trava em
    per_page=5 e a API não ordena por padrão -- como o code "COLETA
    QUATRO ESTRELAS" se repete em dezenas de registros históricos (não
    é único nessa conta), uma busca só por code com per_page baixo
    pode nunca trazer o registro de hoje entre os 5 primeiros que a
    API decidir devolver (achado em teste, 20/08: devolvia sempre um
    registro de meses atrás). Filtrando direto por customer_id + type
    + janela de created_at de hoje, o resultado fica correto
    independente de quantos registros antigos existem."""
    inicio_hoje = hoje.strftime("%Y-%m-%d") + " 00:00:00"
    fim_hoje = (hoje + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    resp = chamar_com_retry(
        vuupt.session.get,
        "https://app.vuupt.com/api/v1/services",
        params={
            "filter[0][field]": "customer_id",
            "filter[0][operator]": "eq",
            "filter[0][value]": CUSTOMER_ID_COLETA,
            "filter[1][field]": "type",
            "filter[1][operator]": "eq",
            "filter[1][value]": "pickup",
            "filter[2][field]": "created_at",
            "filter[2][operator]": "gte",
            "filter[2][value]": inicio_hoje,
            "filter[3][field]": "created_at",
            "filter[3][operator]": "lt",
            "filter[3][value]": fim_hoje,
            "per_page": 5,
        },
        timeout=20,
    )
    resp.raise_for_status()
    dados = resp.json().get("data", [])
    return dados[0] if dados else None


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _carregar_estado() -> dict:
    if not ESTADO_PATH.exists():
        return {"contagem": {}}
    try:
        return json.loads(ESTADO_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Falha ao ler {ESTADO_PATH} ({e}) -- recomeçando contagem do zero.")
        return {"contagem": {}}


def _salvar_estado(estado: dict) -> None:
    ESTADO_PATH.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def _motorista_do_dia(hoje: date, contagem: dict) -> tuple[int, str]:
    """Decide qual motorista coleta hoje: aplica o rodízio fixo
    (segunda exclui Vinícius, sexta exclui Erick) e, entre os
    elegíveis, escolhe quem tem menos coletas acumuladas em `contagem`
    (chaves como string do agent_id) -- empate por agent_id, só pra
    ser determinístico."""
    dia_semana = hoje.weekday()  # segunda=0 ... domingo=6
    if dia_semana == 0:
        elegiveis = [AGENT_ID_ERICK]
    elif dia_semana == 4:
        elegiveis = [AGENT_ID_VINICIUS]
    else:
        elegiveis = [AGENT_ID_VINICIUS, AGENT_ID_ERICK]

    escolhido = min(elegiveis, key=lambda aid: (contagem.get(str(aid), 0), aid))
    return escolhido, NOMES_MOTORISTAS[escolhido]


def main(modo_teste: bool = False):
    inicio = time.time()
    prefixo = "[MODO TESTE] " if modo_teste else ""
    logger.info(f"{prefixo}Lançamento de coleta Empório Quatro Estrelas iniciado.")

    config = _carregar_config()
    resumo_etapas = {}

    try:
        token = config.get("vuupt_api", {}).get("token", "")
        if not token:
            raise RuntimeError("config.yaml sem vuupt_api.token.")

        hoje = date.today()
        if hoje.weekday() >= 5:
            logger.info(f"{prefixo}Hoje ({hoje:%d/%m/%Y}) é fim de semana -- rotina só roda em dias úteis, nada a fazer.")
            resumo_etapas["Coleta Empório Quatro Estrelas"] = {
                "status": "ok", "detalhe": "Fim de semana -- rotina não roda.",
            }
        else:
            estado = _carregar_estado()
            agent_id, nome_motorista = _motorista_do_dia(hoje, estado.get("contagem", {}))

            vuupt = VuuptClient(token)
            ultimo_servico = _buscar_coleta_de_hoje(vuupt, hoje)

            if ultimo_servico:
                logger.info(
                    f"{prefixo}Já existe uma coleta de hoje ({hoje:%d/%m/%Y}) -- service "
                    f"{ultimo_servico['id']} (route {ultimo_servico.get('route_id')}) -- nada a fazer."
                )
                resumo_etapas["Coleta Empório Quatro Estrelas"] = {
                    "status": "ok",
                    "detalhe": f"Já existia (service {ultimo_servico['id']}, route {ultimo_servico.get('route_id')}).",
                }
            else:
                if modo_teste:
                    service_id = None
                    logger.info(f"{prefixo}Criaria o serviço de coleta '{CODIGO_TITULO_COLETA}' (type=pickup) para o Empório Quatro Estrelas.")
                else:
                    payload = {
                        "title": CODIGO_TITULO_COLETA,
                        "code": CODIGO_TITULO_COLETA,
                        "type": "pickup",
                        "customer_id": CUSTOMER_ID_COLETA,
                    }
                    resultado = vuupt.criar_servico(payload)
                    service_id = resultado.get("service", resultado)["id"]
                    logger.info(f"Serviço de coleta criado: service {service_id} ('{CODIGO_TITULO_COLETA}').")

                nome_rota = f"Coleta Empório Quatro Estrelas - {hoje:%d/%m/%Y}"
                start_at = f"{hoje:%Y-%m-%d}T13:00:00Z"

                if modo_teste:
                    logger.info(
                        f"{prefixo}Criaria a rota '{nome_rota}' com o serviço acima, "
                        f"motorista: {nome_motorista} (agent_id={agent_id})."
                    )
                    resumo_etapas["Coleta Empório Quatro Estrelas"] = {
                        "status": "ok", "detalhe": f"[TESTE] Lançaria coleta para {nome_motorista}.",
                    }
                else:
                    rota = criar_rota(
                        token, nome=nome_rota, start_at=start_at,
                        start_location_base_id=BASE_LOCATION_ID, service_ids=[service_id],
                        end_location_base_id=BASE_LOCATION_ID, agent_id=agent_id,
                    )
                    logger.info(f"Rota criada: '{nome_rota}' (route {rota['id']}) -- motorista: {nome_motorista}.")

                    estado.setdefault("contagem", {})
                    estado["contagem"][str(agent_id)] = estado["contagem"].get(str(agent_id), 0) + 1
                    _salvar_estado(estado)

                    resumo_etapas["Coleta Empório Quatro Estrelas"] = {
                        "status": "ok",
                        "detalhe": f"Coleta lançada para {nome_motorista} (service {service_id}, route {rota['id']}). "
                                   f"Total acumulado: {estado['contagem']}.",
                    }

    except Exception as e:
        logger.exception(f"Erro no lançamento de coleta Empório Quatro Estrelas: {e}")
        resumo_etapas["Coleta Empório Quatro Estrelas"] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"{prefixo}Lançamento de coleta finalizado em {duracao:.1f}s.")

    try:
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Lança na VUUPT o pedido de coleta diário do Empório Quatro Estrelas, "
                    "alternando o motorista entre Vinícius e Erick.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--modo-teste", action="store_true",
                        help="Só mostra o que faria, não cria nada na VUUPT nem altera o estado de contagem.")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
