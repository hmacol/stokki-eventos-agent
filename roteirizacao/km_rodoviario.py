# -*- coding: utf-8 -*-
"""
roteirizacao/km_rodoviario.py

Km RODOVIÁRIO de uma rota (base -> paradas na ordem -> base) pela Google
Routes API (computeRoutes v2), com linha reta (haversine) como reserva.

Por que existe (Hugo, 11/09/2026): o km estimado do rascunho era linha
reta (rascunhos_rota.recalcular_km) e subestimava 25-35% o que o
motorista roda de verdade -- e é esse km que define o adicional da
tarifa (regras/tarifa_motorista.py). O Hugo escolheu a Routes API (mesma
chave do Geocoding; até 25 paradas por chamada cai no nível "Pro", com
5.000 chamadas grátis/mês -- muito acima das ~300 rotas/mês recalculadas
algumas vezes cada).

O resultado separa o trecho de VOLTA (última parada -> base) porque, pela
decisão do Hugo, a volta só é paga quando a rota teve insucesso/parcial
ou parada fora da Grande SP (regras/km_cobrado.py).

COMO USAR:
    from km_rodoviario import calcular_km          # roteirizacao/ no sys.path
    r = calcular_km(base=(lat, lng), coords=[(lat, lng), ...], api_key=chave)
    r.total_km, r.ida_km, r.volta_km, r.fonte     # fonte: GOOGLE_ROUTES | HAVERSINE

Falha de rede / chave sem a Routes API habilitada / resposta estranha ->
cai na linha reta e loga um warning (nunca derruba a edição do rascunho).
A Routes API precisa estar HABILITADA no projeto do Google Cloud (console
-> APIs e serviços -> "Routes API"); até lá toda chamada devolve 403 e o
km sai como HAVERSINE -- o extrato marca a fonte, então dá pra ver.
"""
import logging
import math
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

URL_ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
MAX_INTERMEDIARIAS_POR_CHAMADA = 25     # limite da Routes API
TIMEOUT_S = 12

FONTE_GOOGLE = "GOOGLE_ROUTES"
FONTE_HAVERSINE = "HAVERSINE"


@dataclass(frozen=True)
class ResultadoKm:
    total_km: float
    ida_km: float        # base -> p1 -> ... -> pN
    volta_km: float      # pN -> base
    fonte: str

    def como_dict(self) -> dict:
        return {"total_km": self.total_km, "ida_km": self.ida_km, "volta_km": self.volta_km, "fonte": self.fonte}


def distancia_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine -- mesma fórmula de roteirizacao_dados._distancia_km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def calcular_km_linha_reta(base: tuple[float, float], coords: list[tuple[float, float]]) -> ResultadoKm | None:
    if not base or not coords:
        return None
    ida = distancia_km(base[0], base[1], *coords[0])
    for i in range(len(coords) - 1):
        ida += distancia_km(*coords[i], *coords[i + 1])
    volta = distancia_km(*coords[-1], base[0], base[1])
    return ResultadoKm(round(ida + volta, 2), round(ida, 2), round(volta, 2), FONTE_HAVERSINE)


def _ponto(lat: float, lng: float) -> dict:
    return {"location": {"latLng": {"latitude": float(lat), "longitude": float(lng)}}}


def _post(corpo: dict, api_key: str, timeout: float) -> dict:
    """Separado pra teste (mock). Levanta em qualquer erro HTTP."""
    resp = requests.post(
        URL_ROUTES, json=corpo, timeout=timeout,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            # Só o que usamos: sem polyline nem instruções (barateia e encurta a resposta)
            "X-Goog-FieldMask": "routes.distanceMeters,routes.legs.distanceMeters",
        },
    )
    resp.raise_for_status()
    return resp.json()


def _pernas_metros(origem: tuple[float, float], intermediarias: list[tuple[float, float]],
                   destino: tuple[float, float], api_key: str, timeout: float) -> list[int]:
    corpo = {
        "origin": _ponto(*origem),
        "destination": _ponto(*destino),
        "intermediates": [_ponto(*c) for c in intermediarias],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
        "units": "METRIC",
        "languageCode": "pt-BR",
    }
    dados = _post(corpo, api_key, timeout)
    rotas = dados.get("routes") or []
    if not rotas:
        raise ValueError(f"Routes API sem rota na resposta: {str(dados)[:200]}")
    pernas = [int(l.get("distanceMeters") or 0) for l in (rotas[0].get("legs") or [])]
    esperado = len(intermediarias) + 1
    if len(pernas) != esperado:
        raise ValueError(f"Routes API devolveu {len(pernas)} pernas, esperava {esperado}.")
    return pernas


def calcular_km_google(base: tuple[float, float], coords: list[tuple[float, float]], api_key: str,
                       timeout: float = TIMEOUT_S) -> ResultadoKm:
    """base -> coords (na ordem) -> base pela Routes API. Rotas com mais de
    25 paradas são quebradas em chamadas encadeadas (origem da próxima =
    última parada da anterior) e as pernas somadas. Levanta em falha."""
    if not api_key:
        raise ValueError("Sem api_key do Google.")
    pontos = [tuple(base)] + [tuple(c) for c in coords] + [tuple(base)]
    pernas: list[int] = []
    i = 0
    while i < len(pontos) - 1:
        fim = min(i + MAX_INTERMEDIARIAS_POR_CHAMADA + 1, len(pontos) - 1)
        pernas.extend(_pernas_metros(pontos[i], pontos[i + 1:fim], pontos[fim], api_key, timeout))
        i = fim
    volta_m = pernas[-1]
    ida_m = sum(pernas[:-1])
    return ResultadoKm(round((ida_m + volta_m) / 1000.0, 2), round(ida_m / 1000.0, 2), round(volta_m / 1000.0, 2), FONTE_GOOGLE)


def calcular_km(base: tuple[float, float] | None, coords: list[tuple[float, float]], api_key: str | None,
                timeout: float = TIMEOUT_S) -> ResultadoKm | None:
    """Google quando der; senão linha reta (com warning). None sem base/paradas."""
    if not base or not coords:
        return None
    if api_key:
        try:
            return calcular_km_google(base, coords, api_key, timeout)
        except Exception as e:
            logger.warning(f"Routes API falhou ({type(e).__name__}: {str(e)[:160]}) -- km em linha reta.")
    return calcular_km_linha_reta(base, coords)


# ── Trajeto com pedágio (calculadora de frete do portal, 11/09) ───────────────
#
# A cotação do cliente (portal_cliente/cotacao.py) precisa do km do trajeto
# (base -> paradas -> base, com `voltar=True`; o Hugo decidiu em 11/09 que a
# cotação SEMPRE considera o retorno ao galpão) E da estimativa de pedágio,
# que a Routes API devolve quando se pede `extraComputations: TOLLS`.
# Função separada das de cima de propósito: `_post` e a máscara de
# campos são mockados nos testes do núcleo (test_nucleo_fase_a) e não
# devem mudar de assinatura.

FIELD_MASK_PEDAGIO = ("routes.distanceMeters,routes.legs.distanceMeters,"
                      "routes.travelAdvisory.tollInfo,routes.legs.travelAdvisory.tollInfo")


@dataclass(frozen=True)
class ResultadoTrajeto:
    km_total: float             # km_ida + km_volta
    km_ida: float               # origem -> paradas (na ordem)
    km_volta: float             # última parada -> origem (0 com voltar=False)
    pernas_km: tuple            # km de cada perna ATÉ a última parada (sem a volta)
    pedagio_brl: float | None   # None = a API não informou (sem pedágio OU sem TOLLS habilitado)
    fonte: str

    def como_dict(self) -> dict:
        return {"km_total": self.km_total, "km_ida": self.km_ida, "km_volta": self.km_volta,
                "pernas_km": list(self.pernas_km), "pedagio_brl": self.pedagio_brl, "fonte": self.fonte}


def _post_com_mascara(corpo: dict, api_key: str, timeout: float, field_mask: str) -> dict:
    resp = requests.post(
        URL_ROUTES, json=corpo, timeout=timeout,
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": field_mask},
    )
    resp.raise_for_status()
    return resp.json()


def _preco_toll(toll_info: dict | None) -> float | None:
    """tollInfo.estimatedPrice = [{currencyCode, units, nanos}] -- soma os
    valores em BRL (em SP só há BRL, mas não custa filtrar)."""
    if not toll_info:
        return None
    total, achou = 0.0, False
    for p in toll_info.get("estimatedPrice") or []:
        if p.get("currencyCode", "BRL") != "BRL":
            continue
        achou = True
        total += float(p.get("units") or 0) + float(p.get("nanos") or 0) / 1e9
    return round(total, 2) if achou else None


def calcular_trajeto_google(origem: tuple[float, float], paradas: list[tuple[float, float]], api_key: str,
                            timeout: float = TIMEOUT_S, voltar: bool = False) -> ResultadoTrajeto:
    """origem -> paradas (na ordem) [-> origem, com voltar=True], com pedágio
    estimado do trajeto inteiro. Levanta em falha."""
    if not api_key:
        raise ValueError("Sem api_key do Google.")
    if not paradas:
        raise ValueError("Sem paradas.")
    pontos = [tuple(origem)] + [tuple(p) for p in paradas] + ([tuple(origem)] if voltar else [])
    pernas_m: list[int] = []
    pedagio: float | None = None
    i = 0
    while i < len(pontos) - 1:
        fim = min(i + MAX_INTERMEDIARIAS_POR_CHAMADA + 1, len(pontos) - 1)
        corpo = {
            "origin": _ponto(*pontos[i]),
            "destination": _ponto(*pontos[fim]),
            "intermediates": [_ponto(*c) for c in pontos[i + 1:fim]],
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_UNAWARE",
            "extraComputations": ["TOLLS"],
            "units": "METRIC",
            "languageCode": "pt-BR",
        }
        dados = _post_com_mascara(corpo, api_key, timeout, FIELD_MASK_PEDAGIO)
        rotas = dados.get("routes") or []
        if not rotas:
            raise ValueError(f"Routes API sem rota na resposta: {str(dados)[:200]}")
        legs = rotas[0].get("legs") or []
        esperado = fim - i
        if len(legs) != esperado:
            raise ValueError(f"Routes API devolveu {len(legs)} pernas, esperava {esperado}.")
        pernas_m.extend(int(l.get("distanceMeters") or 0) for l in legs)
        p = _preco_toll((rotas[0].get("travelAdvisory") or {}).get("tollInfo"))
        if p is None:
            # Sem resumo na rota: soma o que vier por perna (pode ser nada).
            por_perna = [_preco_toll((l.get("travelAdvisory") or {}).get("tollInfo")) for l in legs]
            p = round(sum(v for v in por_perna if v is not None), 2) if any(v is not None for v in por_perna) else None
        if p is not None:
            pedagio = round((pedagio or 0.0) + p, 2)
        i = fim
    return _montar_trajeto([m / 1000.0 for m in pernas_m], voltar, pedagio, FONTE_GOOGLE)


def _montar_trajeto(pernas_km: list[float], voltar: bool, pedagio: float | None, fonte: str) -> ResultadoTrajeto:
    """Separa a última perna (volta ao ponto de origem) do trecho de ida.

    Arredonda ANTES de somar (e não a soma bruta): a cotação do cliente
    mostra "total = ida + volta" e as pernas por entrega lado a lado, então
    os números exibidos precisam fechar entre si -- somar os brutos deixava
    o total um centésimo de km fora da conta.
    """
    volta = round(pernas_km[-1], 2) if voltar else 0.0
    pernas_ida = tuple(round(k, 2) for k in (pernas_km[:-1] if voltar else pernas_km))
    ida = round(sum(pernas_ida), 2)
    return ResultadoTrajeto(round(ida + volta, 2), ida, volta, pernas_ida, pedagio, fonte)


def calcular_trajeto(origem: tuple[float, float] | None, paradas: list[tuple[float, float]], api_key: str | None,
                     timeout: float = TIMEOUT_S, voltar: bool = False) -> ResultadoTrajeto | None:
    """Google quando der; senão linha reta (sem pedágio, com warning)."""
    if not origem or not paradas:
        return None
    if api_key:
        try:
            return calcular_trajeto_google(origem, paradas, api_key, timeout, voltar)
        except Exception as e:
            logger.warning(f"Routes API (trajeto) falhou ({type(e).__name__}: {str(e)[:160]}) -- km em linha reta.")
    pontos = [tuple(origem)] + [tuple(p) for p in paradas] + ([tuple(origem)] if voltar else [])
    pernas = [distancia_km(*pontos[i], *pontos[i + 1]) for i in range(len(pontos) - 1)]
    return _montar_trajeto(pernas, voltar, None, FONTE_HAVERSINE)
