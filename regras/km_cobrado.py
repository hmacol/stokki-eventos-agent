# -*- coding: utf-8 -*-
"""
regras/km_cobrado.py

Qual km entra na franquia da tarifa do motorista (regras/tarifa_motorista.py).
Decisão do Hugo em 11/09/2026 (seção 4 do DOC_EXECUCAO_CLAUDE_APP_MOTORISTAS.md):

    A rota é paga de base -> paradas. A VOLTA ao CD só conta quando:
      - houve INSUCESSO ou entrega PARCIAL (produto volta pro galpão), ou
      - alguma parada fica fora da Grande SP (RAIO_GRANDE_SP_KM do centro).
    Rota "limpa" e urbana termina na última parada.

Fontes do km (nucleo_rotas):
  - km_real + km_fonte=GPS_APP: soma do GPS do app, que PARA na última
    parada (a rota conclui sozinha no último resultado). Quando a volta
    conta, soma-se o trecho de volta ESTIMADO (km_volta_estimado -- Google
    Routes ou linha reta).
  - km_real + km_fonte=INFORMADO: o motorista digitou o km total; vale
    como está (assume-se que ele contou o que rodou).
  - km_estimado (+ km_volta_estimado): estimativa do rascunho. Volta não
    conta -> desconta km_volta_estimado. Provisório até o app medir.
  - nada: km desconhecido, só a base é paga (tarifa_motorista já trata).

km_volta_estimado ausente (rota antiga, antes de 11/09): calcula a volta em
linha reta da última parada até o CD -- melhor do que ignorar.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

from km_rodoviario import distancia_km          # noqa: E402  (roteirizacao/)
from regioes_dia_fixo import RAIO_GRANDE_SP_KM  # noqa: E402  (roteirizacao/)

# Centro de SP (Praça da Sé) -- mesma origem do raio da Grande SP em
# roteirizacao_dados.COORD_CENTRO_SP / zonas_sp.py / alocacao_motoristas.py.
COORD_CENTRO_SP = (-23.550520, -46.633309)
# CD (Rua Zilda 288, Casa Verde Alta) -- roteirizacao_config desde junho/2026.
COORD_CD = (-23.497039, -46.6605211)

MOTIVO_INSUCESSO = "INSUCESSO"
MOTIVO_PARCIAL = "PARCIAL"
MOTIVO_FORA_GRANDE_SP = "FORA_GRANDE_SP"

FONTE_GPS = "GPS_APP"
FONTE_INFORMADO = "INFORMADO"
FONTE_ESTIMADO = "ESTIMADO"


@dataclass(frozen=True)
class KmCobrado:
    km: float | None                 # o que entra na tarifa (None = desconhecido)
    fonte: str | None                # GPS_APP | INFORMADO | ESTIMADO | None
    provisorio: bool                 # True quando ainda não é medição do app
    volta_conta: bool
    motivo_volta: str | None         # INSUCESSO | PARCIAL | FORA_GRANDE_SP | None
    km_volta: float | None           # trecho de volta considerado (ou descontado)
    volta_estimada: bool             # volta veio de estimativa (GPS não cobre a volta)

    def como_dict(self) -> dict:
        return {
            "km": self.km, "fonte": self.fonte, "provisorio": self.provisorio,
            "volta_conta": self.volta_conta, "motivo_volta": self.motivo_volta,
            "km_volta": self.km_volta, "volta_estimada": self.volta_estimada,
        }


def parada_fora_grande_sp(latitude, longitude, raio_km: float = RAIO_GRANDE_SP_KM) -> bool:
    if latitude is None or longitude is None:
        return False
    return distancia_km(float(latitude), float(longitude), *COORD_CENTRO_SP) > raio_km


def motivo_da_volta(paradas: list[dict]) -> str | None:
    """`paradas`: dicts com situacao, latitude, longitude (paradas CANCELADAS
    já filtradas pelo chamador). Primeiro motivo encontrado, na ordem de
    prioridade INSUCESSO > PARCIAL > FORA_GRANDE_SP."""
    situacoes = {str(p.get("situacao") or "") for p in paradas}
    if "INSUCESSO" in situacoes:
        return MOTIVO_INSUCESSO
    if "PARCIAL" in situacoes:
        return MOTIVO_PARCIAL
    if any(parada_fora_grande_sp(p.get("latitude"), p.get("longitude")) for p in paradas):
        return MOTIVO_FORA_GRANDE_SP
    return None


def _km_volta(rota: dict, paradas: list[dict]) -> float | None:
    if rota.get("km_volta_estimado") is not None:
        return float(rota["km_volta_estimado"])
    ultima = next((p for p in reversed(paradas) if p.get("latitude") is not None and p.get("longitude") is not None), None)
    if ultima is None:
        return None
    return round(distancia_km(float(ultima["latitude"]), float(ultima["longitude"]), *COORD_CD), 2)


def calcular_km_cobrado(rota: dict, paradas: list[dict]) -> KmCobrado:
    """`rota`: linha de nucleo_rotas (km_real, km_fonte, km_estimado,
    km_volta_estimado); `paradas`: situacao/latitude/longitude das paradas
    não canceladas, na ordem."""
    motivo = motivo_da_volta(paradas)
    volta_conta = motivo is not None
    km_volta = _km_volta(rota, paradas)

    if rota.get("km_real") is not None:
        real = float(rota["km_real"])
        fonte = rota.get("km_fonte") or FONTE_GPS
        if fonte == FONTE_INFORMADO:
            return KmCobrado(round(real, 2), FONTE_INFORMADO, False, volta_conta, motivo, None, False)
        if volta_conta and km_volta is not None:
            return KmCobrado(round(real + km_volta, 2), FONTE_GPS, False, True, motivo, km_volta, True)
        return KmCobrado(round(real, 2), FONTE_GPS, False, volta_conta, motivo, km_volta if volta_conta else None, False)

    if rota.get("km_estimado") is not None:
        total = float(rota["km_estimado"])
        if volta_conta or km_volta is None:
            return KmCobrado(round(total, 2), FONTE_ESTIMADO, True, volta_conta, motivo, km_volta if volta_conta else None, True)
        return KmCobrado(round(max(0.0, total - km_volta), 2), FONTE_ESTIMADO, True, False, None, km_volta, True)

    return KmCobrado(None, None, True, volta_conta, motivo, None, False)
