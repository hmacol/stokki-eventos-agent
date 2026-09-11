# -*- coding: utf-8 -*-
"""
regras/tarifa_motorista.py

Remuneração do motorista por rota -- regra confirmada pelo Hugo em
25/08/2026 (seção 4 do DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md):

    Fiorino:   R$ 340,00 até 65 km  + R$ 1,00 por km adicional
    HR / Van:  R$ 550,00 até 100 km + R$ 1,00 por km adicional
    VUC:       R$ 700,00 até 120 km + R$ 1,25 por km adicional   (Hugo, 11/09)

A tarifa é do VEÍCULO DO MOTORISTA (coluna TIPO_VEICULO da planilha
BD_MOTORISTAS.xlsx / motoristas.tipo_veiculo), não da classificação da
rota. Coluna vazia = Fiorino (é o veículo padrão da última milha; a
planilha só registra tipo pra veículo grande -- ver
regras/preferencias_motoristas.py). "FIORINO" também é aceito explícito.

3/4 e Truck NÃO têm tarifa definida ainda (Hugo, 11/09: "deixar a
definir"): calcular_valor_rota devolve None e o extrato mostra "a
definir" -- nunca inventa um valor.

Os valores padrão abaixo são o fallback; em produção a fonte editável é
a tabela `tarifas_motorista` (nucleo/banco.py), semeada com estes mesmos
números por `semear_tarifas_padrao`. O mesmo 340/65/1,00 já constava em
`roteirizacao_config` desde o desenho de junho/2026 -- consistente.

Decisões do Hugo em 11/09 sobre o que é "km" (implementadas em
regras/km_cobrado.py e nucleo/financeiro.py, não aqui):
  - a volta ao CD só conta quando a rota teve insucesso/parcial (produto
    volta) ou parada fora da Grande SP; senão a rota termina na última
    parada;
  - km estimado é RODOVIÁRIO (Google Routes API, roteirizacao/km_rodoviario.py),
    linha reta só como reserva;
  - pedágio é reembolsado à parte (foto + valor no app, aprovação no painel);
  - rota com insucesso paga integral; reentrega (-R) é parada normal.
"""
import sqlite3
from dataclasses import dataclass

TIPO_FIORINO = "FIORINO"
TIPO_VAN_HR = "VAN_HR"
TIPO_VUC = "VUC"


@dataclass(frozen=True)
class Tarifa:
    tipo_veiculo: str
    nome: str
    valor_base: float
    km_franquia: float
    valor_km_adicional: float


TARIFAS_PADRAO: dict[str, Tarifa] = {
    TIPO_FIORINO: Tarifa(TIPO_FIORINO, "Fiorino / utilitário pequeno", 340.00, 65.0, 1.00),
    TIPO_VAN_HR: Tarifa(TIPO_VAN_HR, "HR / Van", 550.00, 100.0, 1.00),
    TIPO_VUC: Tarifa(TIPO_VUC, "VUC", 700.00, 120.0, 1.25),
}

# Apelidos aceitos na planilha/cadastro (já normalizados: maiúsculo,
# não-alfanumérico vira '_', mesmo padrão de preferencias_motoristas).
_APELIDOS = {
    "": TIPO_FIORINO, "FIORINO": TIPO_FIORINO, "UTILITARIO": TIPO_FIORINO,
    "VAN_HR": TIPO_VAN_HR, "VAN": TIPO_VAN_HR, "HR": TIPO_VAN_HR, "VAN_HR_": TIPO_VAN_HR,
    "3_4": "TRES_QUARTOS", "34": "TRES_QUARTOS",
}


@dataclass(frozen=True)
class ResultadoTarifa:
    tipo_tarifa: str
    nome_tarifa: str
    valor_base: float
    km_franquia: float
    valor_km_adicional: float
    km_considerado: float | None      # None = km desconhecido (só a base é paga, com aviso)
    km_excedente: float
    valor_adicional: float
    valor_total: float

    @property
    def km_desconhecido(self) -> bool:
        return self.km_considerado is None

    def como_dict(self) -> dict:
        return {
            "tipo_tarifa": self.tipo_tarifa, "nome_tarifa": self.nome_tarifa,
            "valor_base": self.valor_base, "km_franquia": self.km_franquia,
            "valor_km_adicional": self.valor_km_adicional,
            "km_considerado": self.km_considerado, "km_excedente": self.km_excedente,
            "valor_adicional": self.valor_adicional, "valor_total": self.valor_total,
            "km_desconhecido": self.km_desconhecido,
        }


def tipo_tarifa_de(tipo_veiculo: str | None) -> str:
    """Código de tarifa a partir do TIPO_VEICULO do motorista. Vazio/None
    -> FIORINO. Código desconhecido é devolvido como está (vai cair em
    'sem tarifa' no cálculo, nunca em Fiorino por acidente)."""
    bruto = (tipo_veiculo or "").strip().upper().replace("/", "_").replace(" ", "_").replace("-", "_")
    return _APELIDOS.get(bruto, bruto)


def calcular_valor_rota(tipo_veiculo: str | None, km: float | None,
                        tarifas: dict[str, Tarifa] | None = None) -> ResultadoTarifa | None:
    """
    Valor da rota pro motorista. `km` é o km cobrado (real do app quando
    houver, senão o estimado -- quem chama decide e registra a fonte).
    km None: paga só a base e sinaliza km_desconhecido (nunca chuta).
    Devolve None quando o tipo de veículo não tem tarifa cadastrada.
    """
    tabela = tarifas if tarifas is not None else TARIFAS_PADRAO
    tarifa = tabela.get(tipo_tarifa_de(tipo_veiculo))
    if tarifa is None:
        return None

    if km is None:
        excedente = 0.0
    else:
        excedente = max(0.0, float(km) - tarifa.km_franquia)
    adicional = round(excedente * tarifa.valor_km_adicional, 2)
    return ResultadoTarifa(
        tipo_tarifa=tarifa.tipo_veiculo, nome_tarifa=tarifa.nome,
        valor_base=tarifa.valor_base, km_franquia=tarifa.km_franquia,
        valor_km_adicional=tarifa.valor_km_adicional,
        km_considerado=None if km is None else float(km),
        km_excedente=round(excedente, 2), valor_adicional=adicional,
        valor_total=round(tarifa.valor_base + adicional, 2),
    )


# ── Persistência (tabela tarifas_motorista, nucleo/banco.py) ────────────────

def semear_tarifas_padrao(conn: sqlite3.Connection):
    """Insere as tarifas padrão só se ainda não existirem (nunca sobrescreve
    valor editado pelo Hugo)."""
    for t in TARIFAS_PADRAO.values():
        conn.execute("""
            INSERT INTO tarifas_motorista (tipo_veiculo, nome, valor_base, km_franquia, valor_km_adicional, vigencia_inicio)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tipo_veiculo) DO NOTHING
        """, (t.tipo_veiculo, t.nome, t.valor_base, t.km_franquia, t.valor_km_adicional,
              "2026-09-11" if t.tipo_veiculo == TIPO_VUC else "2026-08-25"))
    conn.commit()


def carregar_tarifas(conn: sqlite3.Connection) -> dict[str, Tarifa]:
    """Tarifas ativas do banco; se a tabela estiver vazia, os padrões do
    código (fail-safe, mesmo padrão do resto de regras/)."""
    rows = conn.execute(
        "SELECT tipo_veiculo, nome, valor_base, km_franquia, valor_km_adicional FROM tarifas_motorista WHERE ativo = 1"
    ).fetchall()
    if not rows:
        return dict(TARIFAS_PADRAO)
    return {
        r[0]: Tarifa(r[0], r[1] or r[0], float(r[2]), float(r[3]), float(r[4]))
        for r in rows
    }