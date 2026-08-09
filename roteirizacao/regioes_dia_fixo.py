# -*- coding: utf-8 -*-
"""
regioes_dia_fixo.py

Regiões com dia fixo de entrega (pedido do Hugo, 02/08, definidas por
"direção"/estrada, com geolocalização validando o agrupamento):

  Vale do Paraíba (Suzano, Mogi das Cruzes, Biritiba Mirim, Jacareí, São José dos
  Campos) -- Segunda
  Baixada Santista (Cubatão, São Vicente, Santos, Guarujá, Praia
  Grande) -- Terça
  Sorocaba (Sorocaba, Votorantim, São Roque, Itu, Salto) -- Terça
  Campinas (Campinas, Jundiaí, Valinhos, Vinhedo, Cabreúva) -- Quarta
  Piracicaba (Piracicaba, Americana, Hortolândia, Sumaré) -- Quarta

Cidades fora dessas regiões: se estiverem dentro do raio da Grande SP
(RAIO_GRANDE_SP_KM de São Paulo), entrega normal, sem restrição. Fora
desse raio mas ainda em SP, ou fora do estado inteiro, ver
notificar_area_nao_atendida.py (pedido do Hugo, 02/08: "outras cidades
podem ser atendidas mas com solicitação de orçamento" / "quando
entrega for fora de São Paulo... questionando se seria um Redespacho").

Reaproveita a MESMA infraestrutura de agendamento já usada no resto do
projeto (campo scheduled_start no VUUPT + roteirizacao_dados.py::
elegivel_para_data): quando um pedido novo aparece pra uma cidade com
dia fixo, e ainda não tem scheduled_start nenhum, este módulo calcula
a próxima ocorrência do dia certo e grava isso no VUUPT -- a partir
daí, TODO o resto do sistema (filtro de elegibilidade pra rota, etc.)
já lida com isso automaticamente, sem precisar de lógica nova em
lugar nenhum. Só aplica quando scheduled_start está em branco -- não
sobrescreve um agendamento já definido por outro motivo (mensagem da
Stokki, confirmação por e-mail, etc.), nem fica reagendando indefinidamente.
"""
import logging
import re
import unicodedata
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# dia da semana no formato do date.weekday() (segunda=0 ... domingo=6)
SEGUNDA, TERCA, QUARTA, QUINTA, SEXTA, SABADO, DOMINGO = range(7)

DIAS_NOMES = {SEGUNDA: "Segunda", TERCA: "Terça", QUARTA: "Quarta",
             QUINTA: "Quinta", SEXTA: "Sexta", SABADO: "Sábado", DOMINGO: "Domingo"}

# Regiões confirmadas com o Hugo, 02/08 -- cada uma com nome (pra
# mensagens/logs), dia da semana, e lista de cidades.
REGIOES: list[dict] = [
    {"nome": "Vale do Paraíba", "dia": SEGUNDA,
     "cidades": ["SUZANO", "MOGI DAS CRUZES", "JACAREI", "SAO JOSE DOS CAMPOS"]},
    {"nome": "Baixada Santista", "dia": TERCA,
     "cidades": ["CUBATAO", "SAO VICENTE", "SANTOS", "GUARUJA", "PRAIA GRANDE"]},
    {"nome": "Sorocaba", "dia": TERCA,
     "cidades": ["SOROCABA", "VOTORANTIM", "SAO ROQUE", "ITU", "SALTO"]},
    {"nome": "Campinas", "dia": QUARTA,
     "cidades": ["CAMPINAS", "JUNDIAI", "VALINHOS", "VINHEDO", "CABREUVA"]},
    {"nome": "Piracicaba", "dia": QUARTA,
     "cidades": ["PIRACICABA", "AMERICANA", "HORTOLANDIA", "SUMARE"]},
]

# Raio (km) a partir de São Paulo considerado "Grande SP" -- entrega
# normal, sem restrição de dia. Cidade fora disso E fora de uma
# REGIAO definida acima vai pro fluxo de notificar_area_nao_atendida.py.
RAIO_GRANDE_SP_KM = 70.0
ENDERECO_REFERENCIA_SP = "São Paulo, SP, Brasil"

HORARIO_INICIO_PADRAO = "08:00:00"
HORARIO_FIM_PADRAO = "16:00:00"


def _normalizar_texto(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).upper().strip()


def _montar_indice() -> dict[str, dict]:
    """Índice invertido (cidade normalizada -> {"dia", "regiao"}),
    montado uma vez a partir de REGIOES."""
    indice = {}
    for regiao in REGIOES:
        for cidade in regiao["cidades"]:
            indice[_normalizar_texto(cidade)] = {"dia": regiao["dia"], "regiao": regiao["nome"]}
    return indice


_INDICE_CIDADES = _montar_indice()


def extrair_cidade(servico: dict) -> str | None:
    """
    Extrai o nome da cidade do campo 'address' do serviço VUUPT.
    Formato real: "..., Cidade - UF, CEP[, Brasil]" -- a cidade é o
    texto logo antes de ' - UF,' (UF = 2 letras maiúsculas). Usa a
    ÚLTIMA ocorrência desse padrão no endereço (mais confiável que a
    primeira, evita pegar algo parecido em bairros/logradouros).
    """
    endereco = servico.get("address") or ""
    ocorrencias = list(re.finditer(r",\s*([^,]+?)\s*-\s*[A-Z]{2}\s*,", endereco))
    if not ocorrencias:
        return None
    return ocorrencias[-1].group(1).strip()


def extrair_uf(servico: dict) -> str | None:
    """Extrai a UF (2 letras) do campo 'address', mesmo padrão de
    extrair_cidade -- usado pra saber se o pedido é fora do estado de SP."""
    endereco = servico.get("address") or ""
    ocorrencias = list(re.finditer(r",\s*[^,]+?\s*-\s*([A-Z]{2})\s*,", endereco))
    if not ocorrencias:
        return None
    return ocorrencias[-1].group(1).strip()


def dia_fixo_da_cidade(cidade: str) -> int | None:
    """Retorna o dia da semana fixo (date.weekday()) pra essa cidade,
    ou None se ela não estiver em nenhuma região com dia fixo."""
    info = _INDICE_CIDADES.get(_normalizar_texto(cidade))
    return info["dia"] if info else None


def regiao_da_cidade(cidade: str) -> str | None:
    """Nome da região (pra mensagens/log) que essa cidade pertence, ou
    None se não estiver em nenhuma região com dia fixo."""
    info = _INDICE_CIDADES.get(_normalizar_texto(cidade))
    return info["regiao"] if info else None


def proxima_data_dia_semana(dia_semana_alvo: int, a_partir_de: date) -> date:
    """
    Próxima ocorrência do dia da semana dado, estritamente APÓS
    `a_partir_de` (se hoje já é o dia certo, pega a próxima semana,
    não hoje -- "próxima quarta-feira" não significa "hoje, se hoje já
    for quarta").
    """
    dias_ate = (dia_semana_alvo - a_partir_de.weekday()) % 7
    if dias_ate == 0:
        dias_ate = 7
    return a_partir_de + timedelta(days=dias_ate)


def aplicar_regioes_dia_fixo(servicos: list[dict], vuupt, hoje: date | None = None) -> int:
    """
    Pra cada serviço SEM scheduled_start ainda, cujo endereço bate com
    uma cidade de dia fixo: calcula a próxima data daquele dia da
    semana e grava no VUUPT (scheduled_start/scheduled_end, horário
    08h-16h). Não mexe em pedido que já tem scheduled_start (de
    qualquer origem) -- evita sobrescrever agendamento já definido e
    evita reagendar o mesmo pedido toda vez que essa função roda.

    Retorna quantos pedidos foram atualizados.
    """
    hoje = hoje or date.today()
    atualizados = 0

    for s in servicos:
        if s.get("scheduled_start"):
            continue

        cidade = extrair_cidade(s)
        if not cidade:
            continue

        dia_fixo = dia_fixo_da_cidade(cidade)
        if dia_fixo is None:
            continue

        data_alvo = proxima_data_dia_semana(dia_fixo, hoje)
        scheduled_start = f"{data_alvo.isoformat()}T{HORARIO_INICIO_PADRAO}-03:00"
        scheduled_end = f"{data_alvo.isoformat()}T{HORARIO_FIM_PADRAO}-03:00"

        try:
            vuupt.atualizar_servico(s["id"], {
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
            })
            s["scheduled_start"] = scheduled_start  # reflete no dict em memória também
            s["scheduled_end"] = scheduled_end
            logger.info(f"  {s.get('code')}: cidade '{cidade}' (região {regiao_da_cidade(cidade)}) -- "
                       f"agendado pra {data_alvo.strftime('%d/%m/%Y')} (próxima ocorrência).")
            atualizados += 1
        except Exception as e:
            logger.warning(f"  {s.get('code')}: falha ao aplicar dia fixo de '{cidade}': {e}")

    return atualizados
