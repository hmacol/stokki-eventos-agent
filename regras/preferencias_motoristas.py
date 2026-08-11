# -*- coding: utf-8 -*-
"""
regras/preferencias_motoristas.py

Cadastro de preferências dos motoristas usado pela alocação automática
de rotas (roteirizacao/alocacao_motoristas.py) -- doc de origem:
DOC_EXECUCAO_CLAUDE_ALOCACAO_MOTORISTAS.md.

Fonte primária: planilha dados/BD_MOTORISTAS.xlsx, colunas
    AGENT_ID_VUUPT | VEHICLE_ID_VUUPT | NOME_MOTORISTA | ACEITA_VIAGENS |
    DIAS_DISPONIVEIS | MAX_ROTAS_DIA | ATIVO | ZONAS_PREFERIDAS |
    TELEFONE_MOTORISTA | EMAIL_MOTORISTA | PLACA

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
    for pedaco in re.split(r"[,;/]", str(valor or "")):
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
    return [z.strip().upper() for z in re.split(r"[,;]", str(valor or "")) if z.strip()]


def _parse_int_opcional(valor) -> int | None:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)) or str(valor).strip() == "":
        return None
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return None


def _construir_motorista(registro: dict) -> "MotoristaPreferencias | None":
    """Constrói um MotoristaPreferencias a partir de um dict com as
    colunas da planilha/JSON (chaves já em maiúsculo). Retorna None
    (com aviso no log) se faltar o AGENT_ID_VUUPT -- sem ele o
    motorista não pode ser usado em lugar nenhum."""
    agent_id = _parse_int_opcional(registro.get("AGENT_ID_VUUPT"))
    if agent_id is None:
        logger.warning(f"Linha de motorista sem AGENT_ID_VUUPT válido -- ignorada: {registro}")
        return None

    nome = str(registro.get("NOME_MOTORISTA") or "").strip() or f"Motorista {agent_id}"
    max_rotas_dia = _parse_int_opcional(registro.get("MAX_ROTAS_DIA")) or 1

    telefone = str(registro.get("TELEFONE_MOTORISTA") or "").strip() or None
    email = str(registro.get("EMAIL_MOTORISTA") or "").strip() or None

    placa_bruta = registro.get("PLACA")
    if placa_bruta is None or (isinstance(placa_bruta, float) and pd.isna(placa_bruta)):
        placa = None
    else:
        placa = re.sub(r"[^A-Z0-9]", "", _normalizar_texto(placa_bruta)) or None

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
    )


class CatalogoMotoristas:
    def __init__(self, motoristas: list[MotoristaPreferencias]):
        self.motoristas = motoristas

    @classmethod
    def _carregar_excel(cls, caminho: Path) -> "CatalogoMotoristas":
        df = pd.read_excel(caminho)
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
