# -*- coding: utf-8 -*-
"""
regras/preferencias_motoristas.py

Cadastro de preferências dos motoristas usado pela alocação automática
de rotas (roteirizacao/alocacao_motoristas.py) -- doc de origem:
DOC_EXECUCAO_CLAUDE_ALOCACAO_MOTORISTAS.md.

Fonte primária: planilha dados/BD_MOTORISTAS.xlsx, colunas
    AGENT_ID_VUUPT | VEHICLE_ID_VUUPT | NOME_MOTORISTA | ACEITA_VIAGENS |
    DIAS_DISPONIVEIS | MAX_ROTAS_DIA | ATIVO | ZONAS_PREFERIDAS |
    TELEFONE_MOTORISTA | EMAIL_MOTORISTA | PLACA | TIPO_VEICULO | CPF_MOTORISTA

CPF_MOTORISTA (pedido do Hugo, 22/08): identificador do motorista na
tela compartilhada do marketplace de rotas (regras/ofertas_rota.py,
confirmacao_motoristas/app.py rota /escolher sem token) -- ao
contrário do link pessoal assinado, essa tela não tem nada além do
CPF pra confirmar quem é quem, então o valor completo (11 dígitos) é
usado como identificador, nunca só os 4 últimos como no /escolher/
<token> pessoal. Coluna ainda não existe na planilha real -- até o
Hugo preencher, cpf=None e o motorista simplesmente não aparece pra
identificação na tela compartilhada (mesmo padrão seguro de
TELEFONE_MOTORISTA/PLACA: dado ausente nunca vira acesso "universal").

PLACA (pedido do Hugo, 11/08): placa do veículo do motorista, usada
pela trava de rodízio municipal de SP (ver roteirizacao/rodizio_sp.py
e roteirizacao/alocacao_motoristas.py::selecionar_motorista_equitativo).
Sincronizada automaticamente quando possível por
scripts/sincronizar_placas_motoristas.py (via GET /agents + GET
/vehicles do VUUPT -- só funciona pro motorista que já tem vehicle_id
vinculado ao agente lá; confirmado em 11/08 que a maioria NÃO tem),
com preenchimento manual pra quem a API não resolve. Motorista sem
PLACA cadastrada: nunca é bloqueado por rodízio (dado ausente não
bloqueia -- mesmo padrão do resto do módulo).

TIPO_VEICULO (pedido do Hugo, 15/08): porte do veículo do motorista --
um dos códigos de regras/tipo_veiculo.py (VAN_HR, VUC, TRES_QUARTOS,
TRUCK), usado pela trava de veículo grande em
roteirizacao/alocacao_motoristas.py::selecionar_motorista_equitativo
(ver regras.tipo_veiculo.veiculo_comporta -- veículo de capacidade
maior também serve rota de tipo menor, ex: motorista de Truck serve
rota classificada VUC). A VUUPT não expõe esse dado de forma confiável
(mesmo problema já visto com PLACA), então é 100% preenchimento manual
na planilha -- sem sincronização automática. Motorista com TIPO_VEICULO
vazio ou não reconhecido: assume-se FIORINO (o carro da maioria da
frota); nunca é bloqueado de rota comum (última milha), mas também
nunca é elegível pra rota classificada como veículo grande (FIORINO é
o MENOR tipo, então continua fora, igual a quando isso era None).

TELEFONE_MOTORISTA / EMAIL_MOTORISTA (doc de origem:
DOC_EXECUCAO_CLAUDE_NOTIFICACAO_MOTORISTAS.md): contato usado por
roteirizacao/avisar_motoristas_rotas.py para o aviso de rota (WhatsApp
copiar/colar + e-mail opcional). Colunas ainda não existem na planilha
atual -- ausentes, ficam None (telefone=None -- entra sem número na
mensagem; email=None -- motorista simplesmente não recebe e-mail),
mesmo padrão seguro do resto do módulo.

ZONAS_PREFERIDAS (pedido do Hugo, 10/08): lista separada por vírgula
das zonas da Grande SP que o motorista atende -- mesmas categorias de
roteirizacao/zonas_sp.py (ZONA NORTE, ZONA SUL, ZONA LESTE, ZONA
OESTE, CENTRO, GUARULHOS, ABCD, OSASCO - BARUERI - SANTANA DE
PARNAÍBA - ALPHAVILLE, COTIA - EMBU DAS ARTES - TABOÃO). Trava RÍGIDA
igual à de Viagem: rota de uma zona só é alocada pra motorista que
tenha aquela zona na lista (ver
roteirizacao/alocacao_motoristas.py::selecionar_motorista_equitativo).
Motorista sem NENHUMA zona cadastrada (campo vazio) nunca é elegível
pra rota com zona reconhecida -- cadastro incompleto não deve virar
elegibilidade universal por acidente.
(mesmo padrão de leitura de planilha usado em
regras/clientes_agendamento.py -- coluna localizada por nome
normalizado, arquivo/coluna ausente nunca derruba o pipeline).

Fallback: dados/motoristas_preferencias.json -- lista de objetos com as
MESMAS chaves da planilha (facilita copiar uma linha da planilha pro
JSON e vice-versa), ex:
    [
      {"AGENT_ID_VUUPT": 1234, "VEHICLE_ID_VUUPT": 5678,
       "NOME_MOTORISTA": "João Silva", "ACEITA_VIAGENS": "SIM",
       "DIAS_DISPONIVEIS": "SEGUNDA,TERCA,QUARTA,QUINTA,SEXTA",
       "MAX_ROTAS_DIA": 1, "ATIVO": "SIM"}
    ]

Nem a planilha nem o JSON existem ainda neste projeto (nenhum dado real
de motorista foi fornecido) -- até serem criados, carregar() devolve um
catálogo vazio (com aviso no log), e a alocação automática cria as
rotas sem motorista (agent_id=None), sinalizando [ALERTA_ALOCACAO] --
mesmo comportamento seguro por padrão do resto do projeto.
"""
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from regras.tipo_veiculo import tipo_por_codigo

logger = logging.getLogger(__name__)

# dia da semana no formato de date.weekday() (segunda=0 ... domingo=6)
_DIAS_SEMANA = {
    "SEGUNDA": 0, "SEGUNDAFEIRA": 0,
    "TERCA": 1, "TERCAFEIRA": 1,
    "QUARTA": 2, "QUARTAFEIRA": 2,
    "QUINTA": 3, "QUINTAFEIRA": 3,
    "SEXTA": 4, "SEXTAFEIRA": 4,
    "SABADO": 5,
    "DOMINGO": 6,
}

_VALORES_VERDADEIROS = {"SIM", "S", "TRUE", "VERDADEIRO", "1", "YES"}

# Veículo assumido pra quem está sem TIPO_VEICULO na planilha (Hugo,
# 22/09) -- ver comentário em _construir_motorista.
TIPO_VEICULO_PADRAO = "FIORINO"


@dataclass
class MotoristaPreferencias:
    agent_id: int
    vehicle_id: int | None
    nome: str
    aceita_viagens: bool
    dias_disponiveis: list[int]  # 0=Segunda, ..., 6=Domingo
    max_rotas_dia: int
    ativo: bool
    zonas_preferidas: list[str]  # ver roteirizacao/zonas_sp.py -- vazio = nenhuma zona da Grande SP habilitada
    telefone: str | None = None  # coluna TELEFONE_MOTORISTA -- usado em avisar_motoristas_rotas.py (WhatsApp)
    email: str | None = None  # coluna EMAIL_MOTORISTA -- usado em avisar_motoristas_rotas.py (e-mail de aviso)
    placa: str | None = None  # coluna PLACA -- usado pela trava de rodízio (ver roteirizacao/rodizio_sp.py)
    tipo_veiculo: str | None = None  # coluna TIPO_VEICULO -- código de regras/tipo_veiculo.py, usado pela trava de veículo grande
    cpf: str | None = None  # coluna CPF_MOTORISTA -- só dígitos; identificação do motorista no marketplace de rotas (ver regras/ofertas_rota.py)


def _normalizar_texto(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).upper().strip()


def _parse_bool(valor) -> bool:
    return _normalizar_texto(valor) in _VALORES_VERDADEIROS


def _parse_dias_disponiveis(valor) -> list[int]:
    """'SEGUNDA,TERCA,QUARTA,QUINTA,SEXTA' -> [0,1,2,3,4]. Nome de dia
    não reconhecido é ignorado (log de aviso), não quebra o parse dos
    demais dias da mesma linha."""
    dias = []
    for pedaco in re.split(r"[,;/]", _parse_texto_numerico(valor) or ""):
        nome = re.sub(r"[^A-Z]", "", _normalizar_texto(pedaco))
        if not nome:
            continue
        dia = _DIAS_SEMANA.get(nome)
        if dia is None:
            logger.warning(f"Dia da semana não reconhecido em DIAS_DISPONIVEIS: '{pedaco}' -- ignorado.")
            continue
        dias.append(dia)
    return dias


def _parse_zonas_preferidas(valor) -> list[str]:
    """'ZONA NORTE,GUARULHOS,ABCD' -> ['ZONA NORTE', 'GUARULHOS', 'ABCD']
    -- mesmos identificadores curtos de roteirizacao/zonas_sp.py.
    Não valida contra a lista de zonas conhecidas aqui (evita
    acoplamento circular com zonas_sp.py); zona desconhecida só nunca
    vai bater com nenhuma rota classificada, sem quebrar o carregamento."""
    return [z.strip().upper() for z in re.split(r"[,;]", _parse_texto_numerico(valor) or "") if z.strip()]


def _parse_int_opcional(valor) -> int | None:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)) or str(valor).strip() == "":
        return None
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return None


def _parse_texto_numerico(valor) -> str | None:
    """TELEFONE_MOTORISTA precisa continuar como string mesmo quando
    parece puramente numérico -- mas se a coluna inteira estava vazia
    até essa linha (dtype float64 do pandas), read_excel devolve um
    float pro único valor preenchido (11999998888 -> 11999998888.0),
    e str(valor) carregaria o '.0' junto. Detecta e corta esse
    artefato -- mesmo problema que _parse_int_opcional já resolve pra
    coluna de ID, só que aqui o resultado precisa continuar string."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    texto = str(valor).strip()
    if re.fullmatch(r"-?\d+\.0", texto):
        texto = texto[:-2]
    return texto or None


def _construir_motorista(registro: dict) -> "MotoristaPreferencias | None":
    """Constrói um MotoristaPreferencias a partir de um dict com as
    colunas da planilha/JSON (chaves já em maiúsculo). Retorna None
    (com aviso no log) se faltar o AGENT_ID_VUUPT -- sem ele o
    motorista não pode ser usado em lugar nenhum."""
    agent_id = _parse_int_opcional(registro.get("AGENT_ID_VUUPT"))
    if agent_id is None:
        logger.warning(f"Linha de motorista sem AGENT_ID_VUUPT válido -- ignorada: {registro}")
        return None

    # Célula vazia do Excel chega como NaN (float, truthy): str() direto
    # virava o texto "nan" -- 24 de 28 motoristas com e-mail "nan" e aviso
    # tentando enviar pra ele (achado 14/09). Mesmo tratamento do telefone.
    nome = _parse_texto_numerico(registro.get("NOME_MOTORISTA")) or f"Motorista {agent_id}"
    max_rotas_dia = _parse_int_opcional(registro.get("MAX_ROTAS_DIA")) or 1

    telefone = _parse_texto_numerico(registro.get("TELEFONE_MOTORISTA"))
    email = _parse_texto_numerico(registro.get("EMAIL_MOTORISTA"))

    placa_bruta = registro.get("PLACA")
    if placa_bruta is None or (isinstance(placa_bruta, float) and pd.isna(placa_bruta)):
        placa = None
    else:
        placa = re.sub(r"[^A-Z0-9]", "", _normalizar_texto(placa_bruta)) or None

    cpf_bruto = _parse_texto_numerico(registro.get("CPF_MOTORISTA"))
    cpf_digitos = re.sub(r"\D", "", cpf_bruto) if cpf_bruto else ""
    cpf = cpf_digitos if len(cpf_digitos) == 11 else None
    if cpf_bruto and cpf is None:
        logger.warning(
            f"CPF_MOTORISTA '{cpf_bruto}' do motorista {agent_id} não tem 11 dígitos -- "
            f"tratado como sem CPF cadastrado (não aparece na identificação por CPF)."
        )

    tipo_veiculo_bruto = _parse_texto_numerico(registro.get("TIPO_VEICULO"))
    codigo_tipo_veiculo = re.sub(r"[^A-Z0-9]", "_", _normalizar_texto(tipo_veiculo_bruto)).strip("_") or None
    tipo_veiculo = tipo_por_codigo(codigo_tipo_veiculo)
    if codigo_tipo_veiculo and tipo_veiculo is None:
        logger.warning(
            f"TIPO_VEICULO '{tipo_veiculo_bruto}' não reconhecido pro motorista {agent_id} -- "
            f"tratado como {TIPO_VEICULO_PADRAO} (veículo padrão da última milha)."
        )
    # Célula vazia = FIORINO (Hugo, 22/09): é o carro da maior parte da
    # frota, e a planilha historicamente só registrava tipo pra veículo
    # grande. Não afrouxa elegibilidade -- FIORINO é o MENOR tipo, então
    # continua fora de qualquer rota de veículo grande, igual a quando
    # isso era None. A tarifa também não muda: regras/tarifa_motorista.py
    # já tratava vazio e "FIORINO" como a mesma tarifa.
    if tipo_veiculo is None:
        tipo_veiculo = tipo_por_codigo(TIPO_VEICULO_PADRAO)

    return MotoristaPreferencias(
        agent_id=agent_id,
        vehicle_id=_parse_int_opcional(registro.get("VEHICLE_ID_VUUPT")),
        nome=nome,
        aceita_viagens=_parse_bool(registro.get("ACEITA_VIAGENS")),
        dias_disponiveis=_parse_dias_disponiveis(registro.get("DIAS_DISPONIVEIS")),
        max_rotas_dia=max_rotas_dia,
        ativo=_parse_bool(registro.get("ATIVO")),
        zonas_preferidas=_parse_zonas_preferidas(registro.get("ZONAS_PREFERIDAS")),
        telefone=telefone,
        email=email,
        placa=placa,
        tipo_veiculo=tipo_veiculo.codigo,
        cpf=cpf,
    )


class CatalogoMotoristas:
    def __init__(self, motoristas: list[MotoristaPreferencias]):
        self.motoristas = motoristas

    @classmethod
    def _carregar_excel(cls, caminho: Path) -> "CatalogoMotoristas":
        # dtype=str só na coluna de CPF -- sem isso, o pandas infere a
        # coluna inteira como número (mesmo artefato documentado em
        # _parse_texto_numerico pro TELEFONE_MOTORISTA) e PERDE zero à
        # esquerda de CPF pra sempre (09474142742 -> 9474142742.0 ->
        # 9474142742, 10 dígitos) -- ali dá pra recuperar cortando o
        # ".0", aqui não, o dígito já sumiu antes de qualquer parsing
        # (achado 22/08, testando a sincronização de CPF de verdade).
        df = pd.read_excel(caminho, dtype={"CPF_MOTORISTA": str})
        df.columns = [re.sub(r"[^A-Z0-9_]", "", _normalizar_texto(c)) for c in df.columns]

        motoristas = []
        for _, linha in df.iterrows():
            m = _construir_motorista(linha.to_dict())
            if m:
                motoristas.append(m)
        logger.info(f"Catálogo de motoristas carregado de {caminho}: {len(motoristas)} motorista(s).")
        return cls(motoristas)

    @classmethod
    def _carregar_json(cls, caminho: Path) -> "CatalogoMotoristas":
        registros = json.loads(caminho.read_text(encoding="utf-8"))
        motoristas = []
        for registro in registros:
            registro_normalizado = {str(k).upper(): v for k, v in registro.items()}
            m = _construir_motorista(registro_normalizado)
            if m:
                motoristas.append(m)
        logger.info(f"Catálogo de motoristas carregado de {caminho}: {len(motoristas)} motorista(s).")
        return cls(motoristas)

    @classmethod
    def carregar(cls, caminho_excel: str | Path | None,
                caminho_json_fallback: str | Path | None = None) -> "CatalogoMotoristas":
        """
        Tenta carregar de `caminho_excel` primeiro; se não existir, tenta
        `caminho_json_fallback`. Se nenhum dos dois existir, devolve um
        catálogo vazio (aviso no log) -- a alocação automática segue
        funcionando, só que sem motorista disponível pra nenhuma rota
        (fica registrado como [ALERTA_ALOCACAO]).
        """
        if caminho_excel:
            caminho = Path(caminho_excel)
            if caminho.exists():
                return cls._carregar_excel(caminho)

        if caminho_json_fallback:
            caminho = Path(caminho_json_fallback)
            if caminho.exists():
                return cls._carregar_json(caminho)

        logger.warning(
            f"Nenhuma base de motoristas encontrada (planilha: {caminho_excel!r}, "
            f"fallback JSON: {caminho_json_fallback!r}) -- nenhum motorista será alocado "
            f"automaticamente até a base ser criada."
        )
        return cls([])
