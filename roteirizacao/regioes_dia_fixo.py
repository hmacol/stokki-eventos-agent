# -*- coding: utf-8 -*-
"""
regioes_dia_fixo.py

Regiões com dia fixo de entrega (pedido do Hugo, 02/08, definidas por
"direção"/estrada, com geolocalização validando o agrupamento; 12/08:
regiões passaram a aceitar MAIS DE UM dia da semana, e além de cidades
também há regiões por ENDEREÇO -- galpões/operadores logísticos
identificados por trechos de rua/CEP, ver ENDERECOS_DIA_FIXO):

  Vale do Paraíba (Suzano, Mogi das Cruzes, Biritiba Mirim, Jacareí,
  São José dos Campos) -- Segunda
  Baixada Santista (Cubatão, São Vicente, Santos, Guarujá, Praia
  Grande) -- Terça
  Sorocaba (Sorocaba, Votorantim, São Roque, Itu, Salto) -- Terça
  Campinas (Campinas, Jundiaí, Valinhos, Vinhedo, Cabreúva, Caieiras,
  Cajamar, Franco da Rocha, Francisco Morato, Louveira) -- Quarta
  Piracicaba (Piracicaba, Americana, Hortolândia, Sumaré) -- Quarta
  Barueri (Barueri, Santana do Parnaíba, Jandira) -- Terça e Quinta
  ABCD (Santo André, São Bernardo do Campo, São Caetano do Sul,
  Diadema, Ribeirão Pires, Mauá) -- Segunda, Quarta e Sexta

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
# mensagens/logs), dias da semana (12/08: virou LISTA -- Barueri e ABCD
# recebem em mais de um dia) e lista de cidades. Biritiba Mirim
# incluída em 12/08 (estava no comentário do topo desde 02/08, mas
# faltava na lista efetiva).
#
# "externa" (12/08, pedido do Hugo: "pedidos de Sorocaba não se
# misturariam com pedidos de Barueri"): True = região FORA da Grande SP
# (rota de Viagem, sem limite de distância entre pedidos, motorista que
# aceita viagem); False = região DENTRO da Grande SP que só está aqui
# pelo dia fixo (Barueri, ABCD) -- entrega urbana normal. Quando Barueri
# e ABCD entraram nesta lista (12/08), classificar_rota_viagem passou a
# tratá-los como Viagem por efeito colateral (ela considerava Viagem
# qualquer cidade de REGIOES) -- este flag desfaz isso.
REGIOES: list[dict] = [
    {"nome": "Vale do Paraíba", "dias": [SEGUNDA], "externa": True,
     "cidades": ["SUZANO", "MOGI DAS CRUZES", "BIRITIBA MIRIM", "JACAREI", "SAO JOSE DOS CAMPOS"]},
    {"nome": "Baixada Santista", "dias": [TERCA], "externa": True,
     "cidades": ["CUBATAO", "SAO VICENTE", "SANTOS", "GUARUJA", "PRAIA GRANDE"]},
    {"nome": "Sorocaba", "dias": [TERCA], "externa": True,
     "cidades": ["SOROCABA", "VOTORANTIM", "SAO ROQUE", "ITU", "SALTO"]},
    {"nome": "Campinas", "dias": [QUARTA], "externa": True,
     "cidades": ["CAMPINAS", "JUNDIAI", "VALINHOS", "VINHEDO", "CABREUVA",
                 "CAIEIRAS", "CAJAMAR", "FRANCO DA ROCHA", "FRANCISCO MORATO",
                 "LOUVEIRA"]},
    {"nome": "Piracicaba", "dias": [QUARTA], "externa": True,
     "cidades": ["PIRACICABA", "AMERICANA", "HORTOLANDIA", "SUMARE"]},
    {"nome": "Barueri", "dias": [TERCA, QUINTA], "externa": False,
     "cidades": ["BARUERI", "SANTANA DO PARNAIBA", "JANDIRA"]},
    {"nome": "ABCD", "dias": [SEGUNDA, QUARTA, SEXTA], "externa": False,
     "cidades": ["SANTO ANDRE", "SAO BERNARDO DO CAMPO", "SAO CAETANO DO SUL",
                 "DIADEMA", "RIBEIRAO PIRES", "MAUA"]},
]

# Regiões por ENDEREÇO (pedido do Hugo, 12/08: "cadastrar também uma
# rua específica como Região") -- destinos logísticos (galpões/
# operadores) com dia fixo próprio, identificados por trechos do
# endereço do serviço (nome da rua e/ou CEP, comparados sem acento e em
# maiúsculas). Têm PRIORIDADE sobre a regra da cidade: um pedido pro
# galpão da TAFF em Barueri segue os dias da TAFF, não os de Barueri.
# Endereços levantados no BD_CLIENTES.xlsx (12/08).
ENDERECOS_DIA_FIXO: list[dict] = [
    # Rua Makita Brasil, 300 - Cooperativa, São Bernardo do Campo/SP
    # (galpão Centrosul/Friozem -- endereço confirmado no
    # BD_TRANSPORTADORAS, mesmo da Andrea Belotto/Frezze/Gessy Lopes).
    # São Bernardo é cidade da região ABCD (seg/qua/sex), mas esta regra
    # por endereço tem prioridade: no galpão, só quarta/sexta.
    {"nome": "Centrosul", "dias": [QUARTA, SEXTA],
     "padroes": ["MAKITA BRASIL", "09852-080", "09852080"]},
    # Estrada Francisco Hengles, 591 - Potuvera, Itapecerica da Serra/SP
    {"nome": "Transfrios", "dias": [SEGUNDA, QUARTA],
     "padroes": ["FRANCISCO HENGLES", "06885-160", "06885160"]},
    # Av. Arterial Sul, 451 (tb. Rod. Raposo Tavares km 20,5) - Parque Ipê, São Paulo/SP
    {"nome": "Superfrio/TAC", "dias": [SEGUNDA, QUARTA],
     "padroes": ["ARTERIAL SUL", "05577-300", "05577300"]},
    # Av. Prefeito João Vila Lobos Quero, 1505 - Jardim Belval, Barueri/SP
    {"nome": "TAFF", "dias": [TERCA, QUINTA],
     "padroes": ["VILA LOBOS QUERO", "06422-122", "06422122"]},
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
    """Índice invertido (cidade normalizada -> {"dias", "regiao",
    "externa"}), montado uma vez a partir de REGIOES."""
    indice = {}
    for regiao in REGIOES:
        for cidade in regiao["cidades"]:
            indice[_normalizar_texto(cidade)] = {
                "dias": regiao["dias"], "regiao": regiao["nome"],
                "externa": regiao.get("externa", False),
            }
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


def dias_fixos_da_cidade(cidade: str) -> list[int] | None:
    """Dias da semana fixos (date.weekday()) dessa cidade, ou None se
    ela não estiver em nenhuma região com dia fixo."""
    info = _INDICE_CIDADES.get(_normalizar_texto(cidade))
    return info["dias"] if info else None


def dia_fixo_da_cidade(cidade: str) -> int | None:
    """COMPATIBILIDADE (assinatura antiga, de quando região tinha 1 dia
    só): o primeiro dia fixo da cidade, ou None. Os consumidores que
    restam (classificar_pedido em notificar_area_nao_atendida.py e
    regras/endereco.py) só usam como flag `is not None` -- quem precisa
    dos dias de verdade usa dias_fixos_da_cidade/regra_dia_fixo_do_servico."""
    dias = dias_fixos_da_cidade(cidade)
    return dias[0] if dias else None


def regiao_da_cidade(cidade: str) -> str | None:
    """Nome da região (pra mensagens/log) que essa cidade pertence, ou
    None se não estiver em nenhuma região com dia fixo."""
    info = _INDICE_CIDADES.get(_normalizar_texto(cidade))
    return info["regiao"] if info else None


def regiao_externa_da_cidade(cidade: str) -> str | None:
    """Nome da região EXTERNA (fora da Grande SP -- Sorocaba, Campinas,
    Vale do Paraíba, Baixada Santista, Piracicaba) dessa cidade, ou None
    se a cidade não está em região nenhuma OU está numa região interna
    de dia fixo (Barueri, ABCD -- dentro da Grande SP). É a função certa
    pra decidir Viagem x Grande SP; regiao_da_cidade serve pra dia fixo
    e mensagens."""
    info = _INDICE_CIDADES.get(_normalizar_texto(cidade))
    return info["regiao"] if info and info["externa"] else None


def regra_dia_fixo_do_servico(servico: dict) -> dict | None:
    """
    Resolve a regra de dia fixo que vale pra ESTE serviço, olhando o
    campo 'address': primeiro as regiões por ENDEREÇO (mais
    específicas -- galpão/operador logístico), depois a cidade.

    Retorna {"nome": str, "dias": list[int], "origem": "endereco"|"cidade"}
    ou None se nenhuma regra se aplica (entrega sem restrição de dia).
    """
    endereco_norm = _normalizar_texto(servico.get("address") or "")
    if endereco_norm:
        for regra in ENDERECOS_DIA_FIXO:
            if any(_normalizar_texto(p) in endereco_norm for p in regra["padroes"] if p):
                return {"nome": regra["nome"], "dias": regra["dias"], "origem": "endereco"}

    cidade = extrair_cidade(servico)
    dias = dias_fixos_da_cidade(cidade) if cidade else None
    if dias:
        return {"nome": cidade.title(), "dias": dias, "origem": "cidade"}
    return None


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


def proxima_data_dias_semana(dias_semana: list[int], a_partir_de: date) -> date:
    """Ocorrência mais próxima de QUALQUER um dos dias da lista,
    estritamente após `a_partir_de` -- regiões com mais de um dia fixo
    (ex.: ABCD = seg/qua/sex) entregam no primeiro dia que chegar."""
    return min(proxima_data_dia_semana(d, a_partir_de) for d in dias_semana)


def nomes_dias(dias_semana: list[int]) -> str:
    """Nomes dos dias no plural, pra mensagens: [SEGUNDA] -> "Segundas";
    [SEGUNDA, QUARTA, SEXTA] -> "Segundas, Quartas e Sextas"."""
    nomes = [DIAS_NOMES[d] + "s" for d in sorted(dias_semana)]
    if len(nomes) == 1:
        return nomes[0]
    return ", ".join(nomes[:-1]) + " e " + nomes[-1]


def ajustar_data_por_dia_fixo(servico: dict, data: date) -> tuple[date, dict | None]:
    """
    Valida uma data de entrega JÁ ESCOLHIDA (planilha, confirmação por
    e-mail, mensagem da Stokki, reagendamento...) contra a regra de dia
    fixo do serviço: se a data cai num dia em que a região/galpão não
    recebe, empurra pra próxima data válida a partir dela (13/08 --
    antes só o reagendamento de insucesso fazia isso, cada fluxo por
    conta própria).

    Retorna (data_final, regra) -- regra é None quando nada mudou (sem
    regra pro endereço, ou a data já era um dia válido); quando mudou,
    é o dict de regra_dia_fixo_do_servico ({"nome", "dias", "origem"}),
    que quem chama usa pra logar/notificar o remetente.
    """
    regra = regra_dia_fixo_do_servico(servico)
    if not regra or data.weekday() in regra["dias"]:
        return data, None
    return proxima_data_dias_semana(regra["dias"], data), regra


def aplicar_regioes_dia_fixo(servicos: list[dict], vuupt, hoje: date | None = None) -> list[dict]:
    """
    Pra cada serviço SEM scheduled_start ainda, que caia numa regra de
    dia fixo (cidade da região OU endereço cadastrado -- ver
    regra_dia_fixo_do_servico): calcula a próxima data válida e grava
    no VUUPT (scheduled_start/scheduled_end, horário 08h-16h). Não mexe
    em pedido que já tem scheduled_start (de qualquer origem) -- evita
    sobrescrever agendamento já definido e evita reagendar o mesmo
    pedido toda vez que essa função roda.

    Retorna a lista dos agendamentos feitos (12/08 -- antes era só a
    contagem): [{"servico", "regiao", "dias", "data"}], que quem chama
    usa pra notificar os remetentes (pedido do Hugo, 12/08:
    "notificação aos clientes que o pedido deles foi agendado para a
    data correta" -- notificar_agendamento_dia_fixo.py).
    """
    hoje = hoje or date.today()
    atualizados: list[dict] = []

    for s in servicos:
        if s.get("scheduled_start"):
            continue

        regra = regra_dia_fixo_do_servico(s)
        if not regra:
            continue

        data_alvo = proxima_data_dias_semana(regra["dias"], hoje)
        scheduled_start = f"{data_alvo.isoformat()}T{HORARIO_INICIO_PADRAO}-03:00"
        scheduled_end = f"{data_alvo.isoformat()}T{HORARIO_FIM_PADRAO}-03:00"

        try:
            vuupt.atualizar_servico(s["id"], {
                "scheduled_start": scheduled_start,
                "scheduled_end": scheduled_end,
            })
            s["scheduled_start"] = scheduled_start  # reflete no dict em memória também
            s["scheduled_end"] = scheduled_end
            logger.info(f"  {s.get('code')}: {regra['origem']} '{regra['nome']}' "
                       f"(entrega às {nomes_dias(regra['dias'])}) -- "
                       f"agendado pra {data_alvo.strftime('%d/%m/%Y')} (próxima ocorrência).")
            atualizados.append({"servico": s, "regiao": regra["nome"],
                                "dias": regra["dias"], "data": data_alvo})
        except Exception as e:
            logger.warning(f"  {s.get('code')}: falha ao aplicar dia fixo de '{regra['nome']}': {e}")

    return atualizados
